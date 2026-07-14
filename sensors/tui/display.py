"""Live visual feedback display for the sensors."""

import asyncio
import os
import select
import sys
import termios
import tty
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from datetime import datetime

from rich.console import Console
from rich.live import Live
from rich.table import Table

from sensors.config.schema import RunnerConfig
from sensors.events import DisplayEvents
from sensors.persistence.models import RunnerEntry
from sensors.persistence.state_manager import StateManager


@contextmanager
def _raw_tty(fd: int) -> Iterator[None]:
    """Put stdin in cbreak/no-echo mode for immediate key reads; restore on exit."""
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        new_settings = termios.tcgetattr(fd)
        new_settings[3] = new_settings[3] & ~termios.ECHO
        termios.tcsetattr(fd, termios.TCSANOW, new_settings)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


class DisplayManager:
    """Manages live terminal display showing runner status."""

    def __init__(  # noqa: PLR0913
        self,
        state_manager: StateManager,
        runner_configs: list[RunnerConfig] | None = None,
        update_interval: float = 1.0,
        events: DisplayEvents | None = None,
        *,
        attach: bool = False,
        on_snapshot: Callable[[], Awaitable[None]] | None = None,
        on_clear: Callable[[], Awaitable[None]] | None = None,
        on_rerun: Callable[[str], Awaitable[None]] | None = None,
        on_shutdown: Callable[[], Awaitable[None]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        """Initialize the display manager.

        Args:
            state_manager: State manager to read current state from
            runner_configs: List of runner configs (used to display mode info)
            update_interval: How often to refresh the display (seconds)
            events: Events used to signal actions to the orchestrator
            attach: If True, action keys delegate to callbacks (remote RPC) instead of
                setting local events. W always closes viewer; Q always stops sensors.
            on_snapshot: Async callback for S key in attach mode.
            on_clear: Async callback for C key in attach mode.
            on_rerun: Async callback for digit keys in attach mode; receives runner name.
            on_shutdown: Async callback for Q key in attach mode.
            clock: Injectable "now" provider used for relative-time rendering ("2s ago").
                Defaults to the real UTC clock; golden-view tests inject a fixed clock so
                the rendered table is byte-deterministic.
        """
        from sensors.time_util import utc_now

        self.state_manager = state_manager
        self.update_interval = update_interval
        self._clock: Callable[[], datetime] = clock or utc_now
        self.console = Console()
        self._should_stop = False
        self._snapshot_status: str | None = None
        self._events = events or DisplayEvents()
        self._clear_status: str | None = None
        self._runner_modes: dict[str, str] = {}
        self._runner_enabled: dict[str, bool] = {}
        self._active_runner_names: set[str] | None = None
        self._runner_display_order: list[str] = []
        self._rerun_events: dict[str, asyncio.Event] = {}
        #: Local wall time when the user triggered a ``mode: triggered`` runner (digit key); cleared
        #: when persisted ``lastRun`` is >= this time so Details can show "Running…" immediately.
        self._triggered_run_started_at: dict[str, datetime] = {}
        self._attach = attach
        self._on_snapshot = on_snapshot
        self._on_clear = on_clear
        self._on_rerun = on_rerun
        self._on_shutdown = on_shutdown
        self.set_runner_configs(runner_configs)

    @staticmethod
    def _mode_display_label(rc: RunnerConfig) -> str:
        """Human-readable repeat column label for a runner config."""
        match rc.mode:
            case "watch":
                return "watch"
            case "triggered":
                return "trigger"
            case "on_check":
                return "on_check"
            case _:
                if rc.interval:
                    return f"{rc.interval / 1000:g}s"
                return "interval"

    def _register_runner_config(self, rc: RunnerConfig) -> None:
        self._runner_enabled[rc.name] = rc.enabled
        self._runner_modes[rc.name] = self._mode_display_label(rc)
        if rc.mode == "on_check":
            self._on_check_runners.add(rc.name)

    def set_runner_configs(
        self,
        runner_configs: list[RunnerConfig] | None,
        *,
        active_runner_names: set[str] | None = None,
        rerun_events: dict[str, asyncio.Event] | None = None,
    ) -> None:
        """Update the runner mode display info from configs.

        ``active_runner_names`` is the set of runner names that actually got a GenericRunner
        (enabled + parser registered). Enabled runners not in this set are skipped at startup
        (e.g. stale ``uv tool install`` missing a parser) — the table shows that explicitly
        instead of endless \"Waiting to start...\".

        ``rerun_events`` maps runner name to the asyncio.Event used for digit-key re-runs
        (interval/triggered modes in the main sensors process only).
        """
        self._runner_modes = {}
        self._runner_enabled = {}
        self._on_check_runners: set[str] = set()
        self._active_runner_names = active_runner_names
        self._rerun_events = dict(rerun_events) if rerun_events else {}
        self._triggered_run_started_at = {}
        if runner_configs:
            self._runner_display_order = [rc.name for rc in runner_configs]
            for rc in runner_configs:
                self._register_runner_config(rc)
        else:
            self._runner_display_order = []

    def _format_time_ago(self, timestamp: datetime) -> str:
        """Format a timestamp as 'X seconds/minutes ago'."""
        # Compare via POSIX timestamps so naive local state times and aware UTC clock
        # values are handled consistently. The clock is injected (see __init__) so the
        # human view is deterministic under a frozen clock in golden tests.
        seconds = int(self._clock().timestamp() - timestamp.timestamp())
        if seconds < 0:
            seconds = 0

        if seconds < 60:
            return f"{seconds}s ago"
        elif seconds < 3600:
            minutes = seconds // 60
            return f"{minutes}m ago"
        else:
            hours = seconds // 3600
            return f"{hours}h ago"

    def _format_time_short(self, timestamp: datetime) -> str:
        """Format a timestamp as HH:MM:SS in the user's local timezone."""
        from sensors.time_util import format_local_short

        return format_local_short(timestamp)

    _STATUS_ICONS: dict[str, str] = {
        "success": "🟢",
        "failure": "🔴",
        "below_threshold": "🟡",
    }

    def _get_status_icon(self, status: str) -> str:
        """Get a colored status icon for a runner status."""
        return self._STATUS_ICONS.get(status, "[dim]?[/dim]")

    def _get_trend_indicator(self, runner_name: str, current_score, snapshot) -> str:
        """Get an emoji trend indicator comparing current score to snapshot."""
        if current_score is None or snapshot is None:
            return "➖"
        snap_runner = snapshot.runners.get(runner_name)
        snap_score = snap_runner.reading.score if (snap_runner and snap_runner.reading) else None
        if snap_score is None:
            return "➖"

        cur = current_score.value
        snap = snap_score.value
        direction = current_score.direction

        if cur == snap:
            return "➡️"

        improving = (cur < snap and direction == "less") or (cur > snap and direction == "more")

        if improving:
            return "🚀"
        else:
            return "🔺"

    @staticmethod
    def _last_run_finished_trigger(last_run: datetime, started: datetime) -> bool:
        """True if persisted last run is from on/after the trigger (command finished)."""
        lr = last_run.replace(tzinfo=None) if last_run.tzinfo else last_run
        st = started.replace(tzinfo=None) if started.tzinfo else started
        return lr >= st

    def _maybe_clear_triggered_running(self, state, runner_name: str) -> bool:
        """Drop trigger overlay if state shows a new result; return True if still running."""
        started = self._triggered_run_started_at.get(runner_name)
        if started is None:
            return False
        rs = state.runners.get(runner_name)
        if rs is not None and self._last_run_finished_trigger(rs.lastRun, started):
            del self._triggered_run_started_at[runner_name]
            return False
        return True

    def _get_score_delta(self, runner_name: str, current_score, snapshot) -> tuple:
        """Get a formatted score delta and whether it's improving.

        Returns (delta_str, improving) or ("", None) if no comparison available.
        """
        if current_score is None or snapshot is None:
            return "", None
        snap_runner = snapshot.runners.get(runner_name)
        snap_score = snap_runner.reading.score if (snap_runner and snap_runner.reading) else None
        if snap_score is None:
            return "", None

        cur = current_score.value
        snap = snap_score.value
        if cur == snap:
            return "", None

        # +/- reflects actual numeric change
        diff = cur - snap
        delta_str = f"(+{diff})" if diff > 0 else f"({diff})"
        improving = (diff < 0 and current_score.direction == "less") or \
                    (diff > 0 and current_score.direction == "more")
        return delta_str, improving

    def _create_table(self, snapshot_time: str | None = None) -> Table:
        """Create a Rich table showing current runner status."""
        title = "[bold]Sensors Status[/bold]"
        if snapshot_time:
            title += f"  [dim]snapshot {snapshot_time}[/dim]"
        table = Table(title=title, show_header=True, header_style="bold cyan")
        table.add_column("#", width=3, justify="right")
        table.add_column("Sensor", style="white", width=12)
        table.add_column("When", width=7)
        table.add_column("St", width=3)
        table.add_column("Trend", width=6, justify="center")
        table.add_column("Last Run", width=12)
        table.add_column("Details", min_width=20)

        return table

    @staticmethod
    def _on_check_row_cells() -> tuple[str, str, str, str]:
        return "[dim]·[/dim]", "", "", "[dim]Runs on `sensors check`[/dim]"

    def _triggered_running_row_cells(
        self, runner_state: RunnerEntry | None,
    ) -> tuple[str, str, str, str]:
        last_run = self._format_time_ago(runner_state.lastRun) if runner_state else ""
        return "[dim]⏳[/dim]", "➖", last_run, "[dim]⏳ Running…[/dim]"

    def _state_runner_row_cells(
        self, runner_name: str, runner_state: RunnerEntry, state,
    ) -> tuple[str, str, str, str]:
        status_icon = self._get_status_icon(runner_state.status)
        last_run = self._format_time_ago(runner_state.lastRun)
        reading = runner_state.reading
        details = (reading.formatted.summary_terminal if reading else "") or "[dim]No details[/dim]"
        score = reading.score if reading else None
        delta_str, improving = self._get_score_delta(runner_name, score, state.snapshot)
        if delta_str:
            color = "green" if improving else "red"
            details = f"{details} [{color}]{delta_str}[/{color}]"
        trend = self._get_trend_indicator(runner_name, score, state.snapshot)
        return status_icon, trend, last_run, details

    def _pending_runner_row_cells(self, runner_name: str) -> tuple[str, str, str, str]:
        if not self._runner_enabled.get(runner_name, True):
            return "[dim]⊘[/dim]", "", "", "[dim]Disabled (enabled: false)[/dim]"
        if self._active_runner_names is not None and runner_name not in self._active_runner_names:
            return (
                "[yellow]![/yellow]",
                "",
                "",
                "[yellow]Not running — parser not loaded (reinstall sensors CLI?) "
                "or runner skipped at startup. Check console for warnings.[/yellow]",
            )
        return "[dim]⏳[/dim]", "", "", "[dim]Waiting to start...[/dim]"

    def _runner_row_cells(
        self,
        runner_name: str,
        state,
        *,
        runner_state: RunnerEntry | None = None,
        on_check: bool = False,
    ) -> tuple[str, str, str, str]:
        """Build status icon, trend, last run, and details for one table row."""
        if on_check:
            return self._on_check_row_cells()
        if self._maybe_clear_triggered_running(state, runner_name):
            return self._triggered_running_row_cells(runner_state)
        if runner_state is not None:
            return self._state_runner_row_cells(runner_name, runner_state, state)
        return self._pending_runner_row_cells(runner_name)

    def _add_configured_runners(self, table: Table, state, shown: set[str]) -> None:
        """Add table rows for all configured runners."""
        for row_index, runner_name in enumerate(self._runner_modes.keys()):
            shown.add(runner_name)
            mode = self._runner_modes.get(runner_name, "?")
            runner_state = state.runners.get(runner_name)
            cells = self._runner_row_cells(
                runner_name,
                state,
                runner_state=runner_state,
                on_check=runner_name in self._on_check_runners,
            )
            table.add_row(str(row_index + 1), runner_name, f"[dim]{mode}[/dim]", *cells)

    def _add_extra_runners(self, table: Table, state, shown: set[str]) -> None:
        """Add table rows for runners in state but not in config."""
        extra_offset = len(self._runner_modes)
        for j, (runner_name, runner_state) in enumerate(
            (name, rs) for name, rs in state.runners.items() if name not in shown
        ):
            mode = self._runner_modes.get(runner_name, "?")
            cells = self._runner_row_cells(runner_name, state, runner_state=runner_state)
            table.add_row(str(extra_offset + j + 1), runner_name, f"[dim]{mode}[/dim]", *cells)

    async def _populate_table(self, table: Table, state=None) -> None:
        """Populate the table with current state data.

        Shows all configured runners, even those that haven't reported yet.
        """
        try:
            if state is None:
                state = await self.state_manager.read_state()

            if not self._runner_modes and not state.runners:
                table.add_row("", "[dim]No runners active[/dim]", "", "", "", "", "")
                return

            shown: set[str] = set()
            self._add_configured_runners(table, state, shown)
            self._add_extra_runners(table, state, shown)
        except Exception as e:
            table.add_row("", "[red]Error[/red]", "", "", "", "", f"[red]{e}[/red]")

    def _check_keypress(self, fd: int) -> str | None:
        """Non-blocking check for a single keypress. Returns the char or None."""
        if select.select([sys.stdin], [], [], 0)[0]:
            return os.read(fd, 1).decode("utf-8", errors="ignore")
        return None

    def _trigger_rerun_if_digit(self, ch: str | None) -> None:
        """If ``ch`` is 1–9, signal the corresponding runner's re-run event (if any)."""
        if not ch or ch not in "123456789":
            return
        idx = int(ch) - 1
        if idx >= len(self._runner_display_order):
            return
        name = self._runner_display_order[idx]
        ev = self._rerun_events.get(name)
        if ev is not None:
            ev.set()
            if self._runner_modes.get(name) == "trigger":
                self._triggered_run_started_at[name] = datetime.now()

    async def _handle_shutdown_key(self) -> None:
        if self._attach:
            if self._on_shutdown is not None:
                await self._on_shutdown()
            self._should_stop = True
        else:
            self._events.shutdown.set()

    async def _handle_snapshot_key(self) -> None:
        if self._attach and self._on_snapshot is not None:
            await self._on_snapshot()
        else:
            self._events.snapshot.set()

    async def _handle_clear_key(self) -> None:
        if self._attach:
            if self._on_clear is not None:
                await self._on_clear()
        else:
            self._clear_status = "Clearing..."
            self._events.clear.set()

    async def _handle_rerun_digit_key(self, ch: str) -> None:
        if self._attach:
            idx = int(ch) - 1
            if idx < len(self._runner_display_order) and self._on_rerun is not None:
                await self._on_rerun(self._runner_display_order[idx])
        else:
            self._trigger_rerun_if_digit(ch)

    async def _handle_key(self, ch: str) -> None:
        """Handle a single keypress; sets flags/events or invokes attach callbacks."""
        ch_lower = ch.lower()
        if ch_lower == "w":
            self._should_stop = True
        elif ch_lower == "q":
            await self._handle_shutdown_key()
        elif ch_lower == "s":
            await self._handle_snapshot_key()
        elif ch_lower == "c":
            await self._handle_clear_key()
        elif ch in "123456789":
            await self._handle_rerun_digit_key(ch)

    def _build_status_line(self):
        """Assemble the keyboard hints and transient status messages."""
        from rich.text import Text

        hint = (
            "[dim]Press [bold]1[/bold]-[bold]9[/bold] re-run row  "
            "[bold]S[/bold] snapshot  [bold]C[/bold] clear & restart  "
            "[bold]W[/bold] close viewer  [bold]Q[/bold] quit and stop sensors[/dim]"
        )
        status_parts = [hint]
        if self._snapshot_status:
            status_parts.append(f"[green]{self._snapshot_status}[/green]")
        if self._clear_status:
            status_parts.append(f"[yellow]{self._clear_status}[/yellow]")
        return Text.from_markup("  │  ".join(status_parts))

    async def run(self) -> None:
        """Run the live display until stopped."""
        from rich.console import Group

        fd = sys.stdin.fileno()
        with _raw_tty(fd), Live(self._create_table(), console=self.console, refresh_per_second=1) as live:
            while not self._should_stop:
                try:
                    ch = self._check_keypress(fd)
                    if ch:
                        await self._handle_key(ch)
                except OSError:
                    pass

                state = await self.state_manager.read_state()
                snapshot_time = self._format_time_short(state.snapshot.timestamp) if state.snapshot else None
                table = self._create_table(snapshot_time)
                await self._populate_table(table, state)
                live.update(Group(table, self._build_status_line()))
                await asyncio.sleep(self.update_interval)

    def stop(self) -> None:
        """Stop the display manager."""
        self._should_stop = True
