"""Render sensors state to terminal text.

This is the human-view rendering seam factored out of the live TUI loop so it can be
driven headlessly — by golden-view tests and, later, a preview command. It builds the
exact same Rich table ``DisplayManager`` shows, then exports it to text (with ANSI
styling by default) so the output *is* what a person sees in the terminal.
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import Callable
from datetime import datetime

from rich.console import Console

from sensors.config.schema import RunnerConfig
from sensors.persistence.models import StateEntry
from sensors.tui.display import DisplayManager


def render_state_to_text(
    state: StateEntry,
    runner_configs: list[RunnerConfig] | None = None,
    *,
    clock: Callable[[], datetime] | None = None,
    width: int = 100,
    styles: bool = True,
) -> str:
    """Render *state* as the human-view table and return it as terminal text.

    Args:
        state: The state to render (runners, snapshot, timestamps).
        runner_configs: Runner configs, for mode labels and display order.
        clock: Injectable "now" provider for relative times ("2s ago"). Pass a fixed
            clock to make output deterministic.
        width: Console width to render at (fixed for deterministic layout).
        styles: When True, include ANSI escape codes so the text renders in color when
            printed to a terminal (the real look). When False, plain text only.
    """
    # file=StringIO so the table is only *recorded* (for export_text) and never written
    # to the real stdout — otherwise rendering would leak the table to the terminal.
    console = Console(
        file=io.StringIO(), record=True, force_terminal=True, width=width, color_system="standard"
    )
    dm = DisplayManager(state_manager=None, runner_configs=runner_configs, clock=clock)  # type: ignore[arg-type]
    dm.console = console
    table = dm._create_table()
    asyncio.run(dm._populate_table(table, state))
    console.print(table)
    return console.export_text(styles=styles)
