"""Shared logic for golden-view "stories".

A *story* is one fixture folder under ``tests/views/`` with up to 5 components:

    INPUTS   case.yaml    the spec: which parser, frozen clock, per-runner timestamps
             <input>      the real captured tool output the parser reads (name per case)
    OUTPUTS  reading.json seam 1 — the normalized SensorReading
             human.txt    seam 2 — the terminal (human) view, with ANSI color
             agent.txt    seam 3 — what `sensors check` shows an agent

Both the pytest harness (``test_views.py``) and the web viewer
(``story_viewer_web.py``) build stories through the functions here, so the two always
agree on how a case is loaded and rendered.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import yaml

from sensors import cli
from sensors.config.result_types import SensorReading
from sensors.config.schema import RunnerConfig
from sensors.persistence.models import RunnerEntry, SnapshotEntry, StateEntry
from sensors.runners.parsers import ParserRegistry
from sensors.time_util import parse_timestamp
from sensors.tui.render import render_state_to_text

VIEWS_DIR = Path(__file__).parent / "views"

OUTPUT_FILES = ("reading.json", "human.txt", "agent.txt")


# --------------------------------------------------------------------------------------
# Loading a case into the view model the real code consumes.
# --------------------------------------------------------------------------------------


def _status_for(reading: SensorReading) -> str:
    """Default status derivation (no threshold): success/failure from the reading."""
    return "success" if reading.success else "failure"


@dataclass
class LoadedCase:
    now: datetime
    state: StateEntry
    configs: list[RunnerConfig]
    input_names: list[str]  # raw-output files referenced by the runners, in order


def load_case(case_dir: Path) -> LoadedCase:
    data = yaml.safe_load((case_dir / "case.yaml").read_text(encoding="utf-8"))
    now = parse_timestamp(data["now"])
    last_updated = parse_timestamp(data.get("lastUpdated", data["now"]))

    configs: list[RunnerConfig] = []
    runners: dict[str, RunnerEntry] = {}
    input_names: list[str] = []
    for r in data["runners"]:
        raw_output = (case_dir / r["input"]).read_text(encoding="utf-8")
        if r["input"] not in input_names:
            input_names.append(r["input"])
        reading = ParserRegistry.get(r["parser"])().parse(raw_output)

        configs.append(
            RunnerConfig(
                name=r["name"],
                parser=r["parser"],
                mode=r["mode"],
                command=r.get("command", r["parser"]),
                interval=r.get("interval"),
                prompt=r.get("prompt"),
            )
        )
        runners[r["name"]] = RunnerEntry(
            lastRun=parse_timestamp(r["lastRun"]),
            status=r.get("status") or _status_for(reading),
            reading=reading,
        )

    snapshot: SnapshotEntry | None = None
    if "snapshot" in data:
        snap = data["snapshot"]
        snap_runners: dict[str, RunnerEntry] = {}
        for r in snap.get("runners", []):
            raw_output = (case_dir / r["input"]).read_text(encoding="utf-8")
            # Snapshot inputs are internal baseline data, not displayed as story panels.
            snap_reading = ParserRegistry.get(r["parser"])().parse(raw_output)
            snap_runners[r["name"]] = RunnerEntry(
                lastRun=parse_timestamp(r["lastRun"]),
                status=r.get("status") or _status_for(snap_reading),
                reading=snap_reading,
            )
        snapshot = SnapshotEntry(
            snapshot_id=snap.get("id", "fixture-snapshot"),
            timestamp=parse_timestamp(snap.get("timestamp", data["now"])),
            runners=snap_runners,
        )

    state = StateEntry(lastUpdated=last_updated, runners=runners, snapshot=snapshot)
    return LoadedCase(now=now, state=state, configs=configs, input_names=input_names)


# --------------------------------------------------------------------------------------
# Rendering the three outputs through the *real* code paths.
# --------------------------------------------------------------------------------------


def render_reading_json(state: StateEntry) -> str:
    """Seam 1 output: the normalized SensorReading, minus the derived `formatted` block
    (that block is exercised by the two view goldens instead)."""
    obj = {
        name: rs.reading.model_dump(mode="json", exclude={"formatted"})
        for name, rs in state.runners.items()
        if rs.reading is not None
    }
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def render_human(case: LoadedCase, *, styles: bool = True) -> str:
    """Seam 2 output: the human/terminal table, with ANSI color when styles=True."""
    return render_state_to_text(
        case.state, case.configs, clock=lambda: case.now, styles=styles
    )


def render_agent(case: LoadedCase) -> str:
    """Seam 3 output: reuse the exact functions `sensors check` prints with."""
    cfg_map = {rc.name: rc for rc in case.configs}
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli._print_check_header(case.state, None, case.now)
        for name, rs in case.state.runners.items():
            cli._print_runner_result(name, rs, cfg_map, case.state, case.now)
    return buf.getvalue()


def render_outputs(case: LoadedCase) -> dict[str, str]:
    """Render all three outputs, keyed by their golden filename."""
    return {
        "reading.json": render_reading_json(case.state),
        "human.txt": render_human(case, styles=True),
        "agent.txt": render_agent(case),
    }


def case_dirs() -> list[Path]:
    """All story folders under tests/views/, sorted by name."""
    return sorted(p.parent for p in VIEWS_DIR.glob("*/case.yaml"))
