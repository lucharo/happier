"""Happier TUI — session manager for Happier CLI."""

from __future__ import annotations

import os

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.reactive import reactive
from textual.widgets import DataTable, Footer, Header, Static

from happier_tui.daemon_client import (
    Session,
    is_daemon_running,
    list_sessions,
    read_daemon_state,
    stop_session,
)


class DaemonStatus(Static):
    """Shows daemon status."""

    daemon_running: reactive[bool] = reactive(False)
    daemon_pid: reactive[int] = reactive(0)
    daemon_port: reactive[int] = reactive(0)
    daemon_version: reactive[str] = reactive("?")
    session_count: reactive[int] = reactive(0)

    def render(self) -> str:
        if not self.daemon_running:
            return "[bold red]● Daemon offline[/]"
        return (
            f"[bold green]● Daemon running[/]  "
            f"PID [cyan]{self.daemon_pid}[/]  "
            f"Port [cyan]{self.daemon_port}[/]  "
            f"v{self.daemon_version}  "
            f"Sessions: [bold]{self.session_count}[/]"
        )


class SessionDetail(Static):
    """Shows details of the selected session."""

    session: reactive[Session | None] = reactive(None)

    def render(self) -> str:
        s = self.session
        if not s:
            return "[dim]Select a session to see details[/]"

        status = "[bold green]running[/]" if s.alive else "[bold red]dead[/]"
        resume_id = s.claude_session_id or s.happier_session_id

        lines = [
            f"[bold]Session Details[/]",
            "",
            f"  Happier ID:  [cyan]{s.happier_session_id}[/]",
            f"  Claude UUID: [cyan]{s.claude_session_id or 'unknown'}[/]",
            f"  PID:         [cyan]{s.pid}[/]",
            f"  Started by:  {s.started_by}",
            f"  Directory:   {s.cwd or 'unknown'}",
            f"  Status:      {status}",
        ]
        if s.title:
            lines.append(f"  Title:       [bold]{s.title}[/]")
        lines.append("")
        lines.append(f"  [dim]Resume cmd:[/] happier --resume {resume_id} --yolo")

        return "\n".join(lines)


class HappierTUI(App):
    """Happier session manager TUI."""

    TITLE = "Happier Sessions"
    CSS = """
    Screen {
        layout: vertical;
    }
    #status-bar {
        height: 3;
        padding: 1;
        background: $surface;
        border-bottom: solid $primary;
    }
    #session-table {
        height: 1fr;
        border: round $primary;
    }
    #detail-panel {
        height: auto;
        max-height: 16;
        padding: 1;
        border-top: solid $primary;
        background: $surface;
    }
    DataTable {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("r", "refresh", "Refresh"),
        Binding("enter", "resume_yolo", "Resume (yolo)"),
        Binding("R", "resume_default", "Resume (default)"),
        Binding("s", "stop_selected", "Stop"),
        Binding("n", "new_session", "New session"),
        Binding("l", "view_logs", "Logs"),
        Binding("q", "quit", "Quit"),
    ]

    sessions: list[Session] = []
    _sessions_by_id: dict[str, Session] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        yield DaemonStatus(id="status-bar")
        with Vertical():
            yield DataTable(id="session-table")
            yield SessionDetail(id="detail-panel")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_type = "row"
        table.add_columns("", "ID", "PID", "Source", "Directory", "Title")
        self.refresh_sessions()
        self.set_interval(5.0, self.refresh_sessions)

    @work(exclusive=True)
    async def refresh_sessions(self) -> None:
        state = read_daemon_state()
        status = self.query_one(DaemonStatus)

        if not state or not is_daemon_running():
            status.daemon_running = False
            status.session_count = 0
            return

        status.daemon_running = True
        status.daemon_pid = state.pid
        status.daemon_port = state.http_port
        status.daemon_version = state.cli_version

        sessions = await list_sessions()
        status.session_count = len(sessions)
        self.sessions = sessions
        self._sessions_by_id = {s.happier_session_id: s for s in sessions}

        table = self.query_one(DataTable)
        table.clear()

        home = os.path.expanduser("~")

        for s in sessions:
            status_icon = "[green]●[/]" if s.alive else "[red]●[/]"
            short_id = s.happier_session_id[:16] + "…"

            cwd = s.cwd or "?"
            if cwd.startswith(home):
                cwd = "~" + cwd[len(home):]

            title = (s.title or "")[:40]

            # Clean up "started_by" display
            source = s.started_by
            if "terminal" in source:
                source = "terminal"
            elif "daemon" in source:
                source = "daemon"

            table.add_row(
                status_icon,
                short_id,
                str(s.pid),
                source,
                cwd,
                title,
                key=s.happier_session_id,
            )

    def _get_selected_session(self) -> Session | None:
        table = self.query_one(DataTable)
        if table.cursor_row is None or not self._sessions_by_id:
            return None
        try:
            row_key, _ = table.coordinate_to_cell_key((table.cursor_row, 0))
            return self._sessions_by_id.get(row_key.value)
        except Exception:
            return None

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        detail = self.query_one(SessionDetail)
        detail.session = None
        detail.session = self._get_selected_session()

    def action_refresh(self) -> None:
        self.refresh_sessions()
        self.notify("Refreshing…")

    def action_resume_yolo(self) -> None:
        session = self._get_selected_session()
        if not session:
            self.notify("No session selected", severity="warning")
            return
        resume_id = session.claude_session_id or session.happier_session_id
        self.exit(result=("resume-yolo", resume_id))

    def action_resume_default(self) -> None:
        session = self._get_selected_session()
        if not session:
            self.notify("No session selected", severity="warning")
            return
        resume_id = session.claude_session_id or session.happier_session_id
        self.exit(result=("resume-default", resume_id))

    @work(exclusive=True)
    async def action_stop_selected(self) -> None:
        session = self._get_selected_session()
        if not session:
            self.notify("No session selected", severity="warning")
            return
        success = await stop_session(session.happier_session_id)
        if success:
            self.notify(f"Stopped {session.happier_session_id[:16]}…")
        else:
            self.notify("Failed to stop session", severity="error")
        self.refresh_sessions()

    def action_new_session(self) -> None:
        self.exit(result=("new", os.getcwd()))

    def action_view_logs(self) -> None:
        session = self._get_selected_session()
        if not session:
            self.notify("No session selected", severity="warning")
            return
        from pathlib import Path
        logs_dir = Path.home() / ".happier" / "logs"
        matches = sorted(logs_dir.glob(f"*-pid-{session.pid}.log"), reverse=True)
        if matches:
            self.exit(result=("logs", str(matches[0])))
        else:
            self.notify("No log file found", severity="warning")

    def action_quit(self) -> None:
        self.exit()


def main() -> None:
    app = HappierTUI()
    result = app.run()

    if result is None:
        return

    action, value = result

    if action == "resume-yolo":
        os.execvp("happier", ["happier", "--resume", value, "--yolo"])
    elif action == "resume-default":
        os.execvp("happier", ["happier", "--resume", value])
    elif action == "new":
        os.execvp("happier", ["happier", "--yolo"])
    elif action == "logs":
        os.execvp("less", ["less", "+G", value])


if __name__ == "__main__":
    main()
