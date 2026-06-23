"""Vitest coverage output parser — reads coverage/coverage-final.json (istanbul/v8 JSON format).

Configure the runner with `result: "coverage/coverage-final.json"` so the runner passes
the JSON file content to parse_output instead of stdout. is_watch_run_complete still
uses stdout line detection for watch mode.
"""

import json
from typing import Any

from sensors.config import Metric, ScoreInfo, SensorReading

from .base import OutputParser


class VitestCovParser(OutputParser):
    """Parser for Vitest coverage-final.json (istanbul JSON format).

    Reads the JSON written by the `json` coverage reporter and computes
    per-file and aggregate statement / branch / function / line percentages.

    Configure the runner with:
        result: "coverage/coverage-final.json"
    """

    def parse_file_error(self, path: str, raw_output: str) -> SensorReading:
        return SensorReading(
            success=False,
            summary="Cannot read results, tests might be failing",
            score=ScoreInfo(value=0, direction="more", description="Branch coverage percentage"),
            extra={"parseError": f"Coverage file not found: {path}"},
        )

    def is_watch_run_complete(self, line: str) -> bool:
        """Detect vitest watch-mode run completion via stdout."""
        import re
        return bool(re.search(r'Tests\s+\d+\s+(failed|passed)', line))

    def parse(self, output: str) -> SensorReading:
        """Parse coverage-final.json (istanbul JSON format) into SensorReading."""
        try:
            data: dict[str, Any] = json.loads(output)
        except (json.JSONDecodeError, ValueError) as e:
            return SensorReading(
                success=False,
                summary=(
                    'Parse error: Expected JSON from coverage/coverage-final.json — '
                    'add `result: "coverage/coverage-final.json"` to your runner config '
                    'and `reporter: ["json"]` to your vitest coverage config.'
                ),
                score=ScoreInfo(value=0, direction="more", description="Branch coverage percentage"),
                extra={"parseError": str(e)},
            )

        try:
            files: list[dict[str, Any]] = []
            totals = {"s": [0, 0], "f": [0, 0], "b": [0, 0]}

            for file_data in data.values():
                file_result = self._parse_file(file_data)
                files.append(file_result)
                for key in ("s", "f", "b"):
                    totals[key][0] += file_result[f"_{key}_hit"]
                    totals[key][1] += file_result[f"_{key}_total"]

            total_stmts = _pct(totals["s"][0], totals["s"][1])
            total_funcs = _pct(totals["f"][0], totals["f"][1])
            total_branch = _pct(totals["b"][0], totals["b"][1])

            clean_files = [{k: v for k, v in f.items() if not k.startswith("_")} for f in files]

            return SensorReading(
                success=True,
                summary=f"{total_branch}% branch",
                score=ScoreInfo(
                    value=int(total_branch),
                    direction="more",
                    description="Branch coverage percentage",
                ),
                metrics=[
                    Metric("totalBranch", "Branch coverage", total_branch, "%", "more", threshold=80.0),
                    Metric("totalStmts", "Statement coverage", total_stmts, "%", "more"),
                    Metric("totalFuncs", "Function coverage", total_funcs, "%", "more"),
                    Metric("totalLines", "Line coverage", total_stmts, "%", "more"),
                ],
                extra={"files": clean_files},
            )
        except Exception as e:
            return SensorReading(
                success=False,
                summary=f"Parse error: {e}",
                score=ScoreInfo(value=0, direction="more", description="Branch coverage percentage"),
                extra={"parseError": str(e)},
            )

    # -- Private helpers --

    def _parse_file(self, file_data: dict[str, Any]) -> dict[str, Any]:
        """Compute per-file coverage percentages from istanbul JSON entry."""
        path = file_data.get("path", "")
        name = path.split("/")[-1] if path else ""

        s: dict[str, int] = file_data.get("s", {})
        f: dict[str, int] = file_data.get("f", {})
        b: dict[str, list[int]] = file_data.get("b", {})
        stmt_map: dict[str, Any] = file_data.get("statementMap", {})

        s_hit = sum(1 for v in s.values() if v > 0)
        s_total = len(s)
        f_hit = sum(1 for v in f.values() if v > 0)
        f_total = len(f)
        b_hit = sum(1 for counts in b.values() for v in counts if v > 0)
        b_total = sum(len(counts) for counts in b.values())

        uncovered_lines = sorted({
            stmt_map[sid]["start"]["line"]
            for sid, count in s.items()
            if count == 0 and sid in stmt_map
        })
        uncovered_str = _format_line_ranges(uncovered_lines)

        return {
            "name": name,
            "path": path,
            "stmts": _pct(s_hit, s_total),
            "branch": _pct(b_hit, b_total),
            "funcs": _pct(f_hit, f_total),
            "lines": _pct(s_hit, s_total),
            "uncovered": uncovered_str,
            # Internal accounting keys (stripped before storage)
            "_s_hit": s_hit, "_s_total": s_total,
            "_f_hit": f_hit, "_f_total": f_total,
            "_b_hit": b_hit, "_b_total": b_total,
        }

# -- Module-level helpers --

def _pct(hit: int, total: int) -> float:
    if total == 0:
        return 100.0
    return round(hit / total * 100, 2)


def _format_line_ranges(lines: list[int]) -> str:
    """Collapse a sorted list of line numbers into a compact range string, e.g. '10-12,15,20-21'."""
    if not lines:
        return ""
    ranges: list[str] = []
    start = end = lines[0]
    for n in lines[1:]:
        if n == end + 1:
            end = n
        else:
            ranges.append(str(start) if start == end else f"{start}-{end}")
            start = end = n
    ranges.append(str(start) if start == end else f"{start}-{end}")
    return ",".join(ranges)
