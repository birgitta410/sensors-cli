"""Interactive terminal story viewer.

Shows the same story structure as the HTML viewer directly in the terminal so
ANSI output is rendered by the terminal itself.

Usage::

    uv run python -m tests.story_viewer_tui
"""

from __future__ import annotations

import os
import select
import sys
import termios
import tty
from collections.abc import Iterator
from contextlib import contextmanager

from rich.console import Console, Group, RenderableType
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from tests.story_lib import serialise_stories


@contextmanager
def _raw_tty(fd: int) -> Iterator[None]:
    """Put stdin in cbreak/no-echo mode; restore terminal settings on exit."""
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        new_settings = termios.tcgetattr(fd)
        new_settings[3] = new_settings[3] & ~termios.ECHO
        termios.tcsetattr(fd, termios.TCSANOW, new_settings)
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


class StoryViewerTUI:
    def __init__(self, stories: list[dict]):
        self.console = Console()
        self.stories = stories
        self.current_idx = 0
        self.sidebar_idx = 0
        self.focus = "sidebar"
        self.sidebar_visible = True
        self.scrolls: dict[str, int] = {
            "inputs": 0,
            "reading": 0,
            "human": 0,
            "agent": 0,
        }
        self.max_scrolls: dict[str, int] = {
            "inputs": 0,
            "reading": 0,
            "human": 0,
            "agent": 0,
        }
        self._should_stop = False

    def _story(self) -> dict | None:
        if not self.stories:
            return None
        return self.stories[self.current_idx]

    def _status_text(self, story: dict | None) -> Text:
        if story is None:
            return Text("no stories", style="yellow")
        if story["allMatch"]:
            return Text("all match", style="green")
        return Text("drift", style="red")

    def _sidebar(self, height: int) -> RenderableType:
        rows: list[Text] = []
        for idx, story in enumerate(self.stories):
            prefix = ">" if idx == self.sidebar_idx else " "
            dot = "*" if story["allMatch"] else "!"
            style = "bold white" if idx == self.sidebar_idx else "dim white"
            rows.append(Text(f"{prefix} {dot} {story['name']}", style=style))

        if not rows:
            rows = [Text("No stories", style="dim")]

        max_visible = max(1, height - 2)
        start = 0
        if self.sidebar_idx >= max_visible:
            start = self.sidebar_idx - max_visible + 1
        visible = rows[start : start + max_visible]
        title = "Stories"
        if self.focus == "sidebar":
            title = "Stories [focused]"
        border = "cyan" if self.focus == "sidebar" else "white"
        return Panel(Group(*visible), title=title, border_style=border)

    def _focus_order(self) -> list[str]:
        order = ["inputs", "reading", "human", "agent"]
        if self.sidebar_visible:
            return ["sidebar", *order]
        return order

    def _ensure_valid_focus(self) -> None:
        order = self._focus_order()
        if self.focus not in order:
            self.focus = order[0]

    @staticmethod
    def _status_badge(cell: dict) -> str:
        status = cell.get("status")
        if status == "match":
            return "[green]approved[/green]"
        if status == "differ":
            return "[red]differs[/red]"
        if status == "unapproved":
            return "[yellow]unapproved[/yellow]"
        return ""

    @staticmethod
    def _cell_lines(cell: dict) -> list[Text]:
        lines: list[Text] = []
        badge = StoryViewerTUI._status_badge(cell)
        header = f"{cell['label']}  ({cell['filename']})"
        if badge:
            header = f"{header}  {badge}"
        lines.append(Text.from_markup(header, style="bold cyan"))

        raw_lines = cell["content"].splitlines()
        if not raw_lines:
            raw_lines = [""]

        for raw in raw_lines:
            if cell["kind"] == "ansi":
                content = Text.from_ansi(raw)
            else:
                content = Text(raw)
            line = Text("  ")
            line.append_text(content)
            lines.append(line)
        lines.append(Text(""))
        return lines

    def _column_panel(
        self,
        *,
        title: str,
        cells: list[dict],
        scroll: int,
        focus_name: str,
        height: int,
    ) -> tuple[RenderableType, int]:
        lines: list[Text] = []
        for cell in cells:
            lines.extend(self._cell_lines(cell))

        if not lines:
            lines = [Text("No content", style="dim")]

        max_visible = max(1, height - 2)
        max_scroll = max(0, len(lines) - max_visible)
        if scroll > max_scroll:
            scroll = max_scroll

        visible = lines[scroll : scroll + max_visible]
        while len(visible) < max_visible:
            visible.append(Text(""))

        suffix = ""
        if max_scroll > 0:
            suffix = f"  lines {scroll + 1}-{min(scroll + max_visible, len(lines))}/{len(lines)}"
        if self.focus == focus_name:
            suffix += "  [focused]"

        border = "cyan" if self.focus == focus_name else "white"
        panel = Panel(Group(*visible), title=f"{title}{suffix}", border_style=border)
        return panel, max_scroll

    def _single_cell_panel(
        self,
        *,
        title: str,
        cell: dict | None,
        focus_name: str,
        height: int,
    ) -> tuple[RenderableType, int]:
        if cell is None:
            panel = Panel(Text("Missing output", style="dim"), title=title, border_style="red")
            return panel, 0
        panel, max_scroll = self._column_panel(
            title=title,
            cells=[cell],
            scroll=self.scrolls[focus_name],
            focus_name=focus_name,
            height=height,
        )
        return panel, max_scroll

    def _render(self) -> RenderableType:
        story = self._story()
        _, height = self.console.size

        if story is None:
            return Panel("No stories found under tests/views/.", title="Sensors Story Viewer")

        header = Text()
        header.append("Sensors Story Viewer", style="bold")
        header.append(f"   {self.current_idx + 1}/{len(self.stories)}  {story['name']}   ")
        header.append_text(self._status_text(story))

        self._ensure_valid_focus()
        body_height = max(9, height - 5)

        input_cells = [c for c in story["cells"] if c["side"] == "input"]
        output_by_name = {c["filename"]: c for c in story["cells"] if c["side"] == "output"}

        inputs, self.max_scrolls["inputs"] = self._column_panel(
            title="Inputs",
            cells=input_cells,
            scroll=self.scrolls["inputs"],
            focus_name="inputs",
            height=body_height,
        )
        self.scrolls["inputs"] = min(self.scrolls["inputs"], self.max_scrolls["inputs"])

        outputs_layout = Layout(name="outputs")
        outputs_layout.split_column(
            Layout(name="reading", ratio=1),
            Layout(name="human", ratio=1),
            Layout(name="agent", ratio=1),
        )

        output_panel_height = max(3, body_height // 3)
        reading_panel, self.max_scrolls["reading"] = self._single_cell_panel(
            title="normalized reading",
            cell=output_by_name.get("reading.json"),
            focus_name="reading",
            height=output_panel_height,
        )
        self.scrolls["reading"] = min(self.scrolls["reading"], self.max_scrolls["reading"])

        human_panel, self.max_scrolls["human"] = self._single_cell_panel(
            title="human view",
            cell=output_by_name.get("human.txt"),
            focus_name="human",
            height=output_panel_height,
        )
        self.scrolls["human"] = min(self.scrolls["human"], self.max_scrolls["human"])

        agent_panel, self.max_scrolls["agent"] = self._single_cell_panel(
            title="agent view",
            cell=output_by_name.get("agent.txt"),
            focus_name="agent",
            height=output_panel_height,
        )
        self.scrolls["agent"] = min(self.scrolls["agent"], self.max_scrolls["agent"])

        outputs_layout["reading"].update(reading_panel)
        outputs_layout["human"].update(human_panel)
        outputs_layout["agent"].update(agent_panel)

        layout = Layout()
        layout.split_column(
            Layout(Panel(header, border_style="white"), name="header", size=3),
            Layout(name="body", ratio=1),
            Layout(name="footer", size=2),
        )

        if self.sidebar_visible:
            layout["body"].split_row(
                Layout(self._sidebar(body_height), name="sidebar", size=24),
                Layout(name="columns", ratio=1),
            )
            columns = layout["body"]["columns"]
        else:
            columns = layout["body"]

        columns.split_row(
            Layout(inputs, name="inputs", ratio=2),
            Layout(outputs_layout, name="outputs", ratio=5),
        )

        help_line = Text.from_markup(
            "[dim]keys:[/dim] [bold]q[/bold] quit  [bold]n/p[/bold] next/prev story  "
            "[bold]Tab[/bold] focus panes  [bold]j/k[/bold] scroll or move  "
            "[bold]Enter[/bold] open selected story  [bold]s[/bold] toggle sidebar"
        )
        layout["footer"].update(Panel(help_line, border_style="white"))

        return layout

    def _read_key(self, fd: int) -> str | None:
        if not select.select([sys.stdin], [], [], 0.1)[0]:
            return None
        raw = os.read(fd, 8).decode("utf-8", errors="ignore")
        if raw.startswith("\x1b[A"):
            return "UP"
        if raw.startswith("\x1b[B"):
            return "DOWN"
        if raw.startswith("\x1b[C"):
            return "RIGHT"
        if raw.startswith("\x1b[D"):
            return "LEFT"
        if raw in ("\r", "\n"):
            return "ENTER"
        if raw == "\t":
            return "TAB"
        return raw

    def _cycle_focus(self) -> None:
        order = self._focus_order()
        idx = order.index(self.focus)
        self.focus = order[(idx + 1) % len(order)]

    def _set_story(self, idx: int) -> None:
        if not self.stories:
            return
        idx = max(0, min(idx, len(self.stories) - 1))
        self.current_idx = idx
        self.sidebar_idx = idx
        for key in self.scrolls:
            self.scrolls[key] = 0

    def _navigate_story(self, delta: int) -> None:
        if not self.stories:
            return
        idx = (self.current_idx + delta + len(self.stories)) % len(self.stories)
        self._set_story(idx)

    def _scroll(self, delta: int) -> None:
        if self.focus == "sidebar":
            self.sidebar_idx = max(0, min(self.sidebar_idx + delta, len(self.stories) - 1))
            return
        if self.focus not in self.scrolls:
            return
        max_scroll = self.max_scrolls.get(self.focus, 0)
        next_scroll = self.scrolls[self.focus] + delta
        self.scrolls[self.focus] = max(0, min(max_scroll, next_scroll))

    def _handle_key(self, key: str) -> None:
        key_l = key.lower()
        if key_l == "q":
            self._should_stop = True
        elif key_l == "n" or key == "RIGHT":
            self._navigate_story(1)
        elif key_l == "p" or key == "LEFT":
            self._navigate_story(-1)
        elif key_l == "s":
            self.sidebar_visible = not self.sidebar_visible
            self._ensure_valid_focus()
        elif key == "TAB":
            self._cycle_focus()
        elif key == "ENTER" and self.focus == "sidebar":
            self._set_story(self.sidebar_idx)
        elif key_l == "j" or key == "DOWN":
            self._scroll(1)
        elif key_l == "k" or key == "UP":
            self._scroll(-1)

    def run(self) -> int:
        fd = sys.stdin.fileno()
        with _raw_tty(fd), Live(
            self._render(), console=self.console, refresh_per_second=10, screen=True
        ) as live:
            while not self._should_stop:
                key = self._read_key(fd)
                if key:
                    self._handle_key(key)
                live.update(self._render())
        return 0


def main() -> int:
    stories = serialise_stories()
    viewer = StoryViewerTUI(stories)
    return viewer.run()


if __name__ == "__main__":
    raise SystemExit(main())
