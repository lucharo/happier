"""HTTP client for the Happier daemon control server."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class DaemonState:
    pid: int
    http_port: int
    started_at: int
    cli_version: str
    control_token: str | None
    log_path: str | None


@dataclass
class Session:
    happier_session_id: str
    pid: int
    started_by: str
    claude_session_id: str | None = None
    title: str | None = None
    cwd: str | None = None
    alive: bool = True


def _find_happier_dir() -> Path:
    return Path.home() / ".happier"


def _find_active_server_dir() -> Path | None:
    happier_dir = _find_happier_dir()
    settings_path = happier_dir / "settings.json"
    if not settings_path.exists():
        return None
    settings = json.loads(settings_path.read_text())
    active_id = settings.get("activeServerId", "cloud")
    server_dir = happier_dir / "servers" / active_id
    return server_dir if server_dir.exists() else None


def read_daemon_state() -> DaemonState | None:
    server_dir = _find_active_server_dir()
    if not server_dir:
        return None
    state_path = server_dir / "daemon.state.json"
    if not state_path.exists():
        return None
    data = json.loads(state_path.read_text())
    return DaemonState(
        pid=data.get("pid", 0),
        http_port=data.get("httpPort", 0),
        started_at=data.get("startedAt", 0),
        cli_version=data.get("startedWithCliVersion", "?"),
        control_token=data.get("controlToken"),
        log_path=data.get("daemonLogPath"),
    )


async def _daemon_post(path: str, body: dict | None = None, timeout: float = 10.0) -> dict:
    import httpx

    state = read_daemon_state()
    if not state or not state.http_port:
        return {"error": "No daemon running"}

    try:
        os.kill(state.pid, 0)
    except OSError:
        return {"error": "Daemon PID not running"}

    headers = {"Content-Type": "application/json"}
    if state.control_token:
        headers["x-happier-daemon-token"] = state.control_token

    url = f"http://127.0.0.1:{state.http_port}{path}"
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, json=body or {}, headers=headers, timeout=timeout)
        if resp.status_code != 200:
            return {"error": f"HTTP {resp.status_code}: {resp.text}"}
        return resp.json()


async def list_sessions() -> list[Session]:
    result = await _daemon_post("/list")
    if "error" in result:
        return []
    children = result.get("children", [])
    sessions = []
    for child in children:
        pid = child.get("pid", 0)
        alive = True
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            alive = False
        except PermissionError:
            alive = True  # Process exists but owned by another user

        s = Session(
            happier_session_id=child.get("happySessionId", ""),
            pid=pid,
            started_by=child.get("startedBy", "unknown"),
            alive=alive,
        )
        sessions.append(s)

    # Enrich all sessions concurrently in background
    await asyncio.gather(*[_enrich_session(s) for s in sessions])
    return sessions


async def stop_session(session_id: str) -> bool:
    result = await _daemon_post("/stop-session", {"sessionId": session_id})
    return result.get("success", False)


async def spawn_session(directory: str) -> dict:
    result = await _daemon_post("/spawn-session", {"directory": directory}, timeout=120.0)
    return result


def _normalize_path(p: str) -> str:
    """Normalize macOS Mutagen paths to local Linux paths."""
    import sys
    if sys.platform != "linux":
        return p
    # On Linux, /Users/* paths are Mutagen-synced macOS paths
    mac_home_match = re.match(r"/Users/[^/]+/(.+)", p)
    if mac_home_match:
        return str(Path.home() / mac_home_match.group(1))
    return p


async def _enrich_session(session: Session) -> None:
    """Find Claude session UUID, title, and cwd. Runs in thread to avoid blocking."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _enrich_session_sync, session)


def _enrich_session_sync(session: Session) -> None:
    """Synchronous enrichment — runs in executor."""
    logs_dir = _find_happier_dir() / "logs"

    # 1. Find Claude session UUID from logs
    if logs_dir.exists():
        try:
            # Only search recent log files (by PID match first, then broader)
            pid_logs = sorted(logs_dir.glob(f"*-pid-{session.pid}.log"), reverse=True)
            for log_file in pid_logs:
                text = log_file.read_text(errors="replace")
                # Look for: Session created/loaded: <happier-id> (tag: <uuid>)
                match = re.search(
                    rf"{re.escape(session.happier_session_id)}.*?tag:\s*([0-9a-f-]{{36}})",
                    text,
                )
                if match:
                    session.claude_session_id = match.group(1)
                    break

            # Fallback: search daemon log
            if not session.claude_session_id:
                daemon_logs = sorted(logs_dir.glob("*-daemon.log"), reverse=True)
                for log_file in daemon_logs[:2]:
                    try:
                        text = log_file.read_text(errors="replace")
                        match = re.search(
                            rf"{re.escape(session.happier_session_id)}.*?tag:\s*([0-9a-f-]{{36}})",
                            text,
                        )
                        if match:
                            session.claude_session_id = match.group(1)
                            break
                    except OSError:
                        pass
        except OSError:
            pass

    # 2. Find title from Claude session JSONL
    if session.claude_session_id:
        # Search all project dirs for the session file
        claude_dir = Path.home() / ".claude" / "projects"
        if claude_dir.exists():
            for project_dir in claude_dir.iterdir():
                session_file = project_dir / f"{session.claude_session_id}.jsonl"
                if session_file.exists():
                    _extract_title(session, session_file)
                    break

    # 3. Get cwd from /proc
    try:
        cwd = os.readlink(f"/proc/{session.pid}/cwd")
        session.cwd = _normalize_path(cwd)
    except OSError:
        pass


def _extract_title(session: Session, session_file: Path) -> None:
    """Extract session title from JSONL — check last lines for summary or title."""
    try:
        # Read last 100 lines
        result = subprocess.run(
            ["tail", "-100", str(session_file)],
            capture_output=True, text=True, timeout=3,
        )
        for line in reversed(result.stdout.splitlines()):
            try:
                msg = json.loads(line)
                # Check for summary message
                if msg.get("type") == "summary" and msg.get("summary"):
                    session.title = msg["summary"][:80]
                    return
                # Check for tool result that set title
                if msg.get("type") == "tool_result":
                    content = msg.get("content", "")
                    if isinstance(content, str) and "changed chat title to" in content.lower():
                        match = re.search(r'title to[:\s]*"?([^"]+)"?', content, re.IGNORECASE)
                        if match:
                            session.title = match.group(1).strip()[:80]
                            return
            except (json.JSONDecodeError, KeyError, TypeError):
                pass
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass


def is_daemon_running() -> bool:
    state = read_daemon_state()
    if not state:
        return False
    try:
        os.kill(state.pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
