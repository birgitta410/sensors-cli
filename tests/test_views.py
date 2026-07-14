"""Golden-view harness: one fixture folder per sensor situation, three approved views.

Each folder under ``tests/views/`` is a self-contained story (see ``story_lib`` for the
5 components). The harness is generic — it never mentions a specific sensor. It runs the
real parser and the real renderers end to end and diffs the result against the approved
files. That is what makes these tests hard to fake: the only way to make one pass is to
actually produce that output.

Workflow:
    # generate / update approved files, then read them to confirm they look right:
    SENSORS_APPROVE=1 uv run pytest tests/test_views.py
    # normal run — asserts current output matches the approved files:
    uv run pytest tests/test_views.py
    # browse the stories in the web viewer:
    uv run python -m tests.story_viewer_web --open
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.story_lib import case_dirs, load_case, render_outputs

APPROVE = os.environ.get("SENSORS_APPROVE") == "1"


def _compare_or_approve(path: Path, actual: str) -> None:
    if APPROVE or not path.exists():
        path.write_text(actual, encoding="utf-8")
        if not APPROVE:
            pytest.skip(f"bootstrapped missing golden {path.name}; re-run to verify")
        return
    expected = path.read_text(encoding="utf-8")
    assert actual == expected, (
        f"{path.parent.name}/{path.name} differs from approved output.\n"
        f"If the change is intended, run: SENSORS_APPROVE=1 uv run pytest tests/test_views.py"
    )


@pytest.mark.parametrize("case_dir", case_dirs(), ids=lambda p: p.name)
def test_view(case_dir: Path) -> None:
    case = load_case(case_dir)
    for name, text in render_outputs(case).items():
        _compare_or_approve(case_dir / name, text)
