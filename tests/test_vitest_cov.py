"""Tests for VitestCovParser — parses coverage/coverage-final.json (istanbul JSON format)."""

import json
from datetime import datetime

import pytest

from sensors.config import RunnerResult
from sensors.runners.parsers.vitest_cov import VitestCovParser, _format_line_ranges

# ---------------------------------------------------------------------------
# Sample data: realistic istanbul coverage-final.json structure
# ---------------------------------------------------------------------------

_DOMAIN_TS = {
    "path": "server/google/domain.ts",
    "statementMap": {
        "0": {"start": {"line": 10, "column": 0}, "end": {"line": 10, "column": 20}},
        "1": {"start": {"line": 11, "column": 0}, "end": {"line": 11, "column": 20}},
        "2": {"start": {"line": 110, "column": 0}, "end": {"line": 110, "column": 20}},
        "3": {"start": {"line": 187, "column": 0}, "end": {"line": 187, "column": 20}},
        "4": {"start": {"line": 249, "column": 0}, "end": {"line": 249, "column": 20}},
    },
    "fnMap": {
        "0": {"name": "handleRequest", "decl": {}, "loc": {}},
        "1": {"name": "parseToken", "decl": {}, "loc": {}},
        "2": {"name": "_unused", "decl": {}, "loc": {}},
    },
    "branchMap": {
        "0": {"type": "if", "locations": [{}, {}]},
        "1": {"type": "if", "locations": [{}, {}]},
    },
    # s: statements 0 and 1 hit; 2,3,4 uncovered
    "s": {"0": 5, "1": 3, "2": 0, "3": 0, "4": 0},
    # f: fn 0 and 1 hit; 2 uncovered
    "f": {"0": 5, "1": 3, "2": 0},
    # b: branch 0 both taken; branch 1 only first taken
    "b": {"0": [3, 2], "1": [1, 0]},
}

_CONFIG_TS = {
    "path": "server/config.ts",
    "statementMap": {
        "0": {"start": {"line": 1, "column": 0}, "end": {"line": 1, "column": 20}},
        "1": {"start": {"line": 2, "column": 0}, "end": {"line": 2, "column": 20}},
    },
    "fnMap": {
        "0": {"name": "getConfig", "decl": {}, "loc": {}},
    },
    "branchMap": {},
    "s": {"0": 10, "1": 8},
    "f": {"0": 10},
    "b": {},
}

_MIDDLEWARE_TS = {
    "path": "server/auth/middleware.ts",
    "statementMap": {
        "0": {"start": {"line": 5, "column": 0}, "end": {"line": 5, "column": 20}},
    },
    "fnMap": {
        "0": {"name": "authMiddleware", "decl": {}, "loc": {}},
    },
    "branchMap": {
        "0": {"type": "if", "locations": [{}, {}]},
    },
    "s": {"0": 7},
    "f": {"0": 7},
    "b": {"0": [7, 5]},
}

SAMPLE_JSON = json.dumps({
    "server/google/domain.ts": _DOMAIN_TS,
    "server/config.ts": _CONFIG_TS,
    "server/auth/middleware.ts": _MIDDLEWARE_TS,
})

SAMPLE_JSON_EMPTY = json.dumps({})


def _make_result(success, **output_fields) -> RunnerResult:
    return RunnerResult(timestamp=datetime.utcnow(), success=success, output=output_fields)


# ---------------------------------------------------------------------------
# parse  (new structured interface)
# ---------------------------------------------------------------------------

def test_parse_returns_parsed_output_success():
    parser = VitestCovParser()
    parsed = parser.parse(SAMPLE_JSON)

    assert parsed.success is True
    assert "branch" in parsed.summary.lower()
    assert parsed.score.direction == "more"


def test_parse_metrics_present():
    parser = VitestCovParser()
    parsed = parser.parse(SAMPLE_JSON)

    keys = {m.key for m in parsed.metrics}
    assert {"totalBranch", "totalStmts", "totalFuncs", "totalLines"} <= keys


def test_parse_branch_metric_has_threshold():
    parser = VitestCovParser()
    parsed = parser.parse(SAMPLE_JSON)

    branch = next(m for m in parsed.metrics if m.key == "totalBranch")
    assert branch.threshold == 80.0
    assert branch.direction == "more"


def test_parse_aggregate_values():
    parser = VitestCovParser()
    parsed = parser.parse(SAMPLE_JSON)

    branch = next(m for m in parsed.metrics if m.key == "totalBranch")
    stmts = next(m for m in parsed.metrics if m.key == "totalStmts")
    funcs = next(m for m in parsed.metrics if m.key == "totalFuncs")

    assert branch.value == pytest.approx(83.33, abs=0.1)
    assert stmts.value == pytest.approx(62.5)
    assert funcs.value == 80.0


def test_parse_per_file_data_in_extra():
    parser = VitestCovParser()
    parsed = parser.parse(SAMPLE_JSON)

    files = parsed.extra["files"]
    assert len(files) == 3
    domain = next(f for f in files if f["name"] == "domain.ts")
    assert domain["stmts"] == 40.0
    assert domain["uncovered"] == "110,187,249"


def test_parse_empty_json():
    parser = VitestCovParser()
    parsed = parser.parse(SAMPLE_JSON_EMPTY)

    assert parsed.success is True
    assert parsed.extra["files"] == []


def test_parse_invalid_json_returns_failure():
    parser = VitestCovParser()
    parsed = parser.parse("not json")

    assert parsed.success is False
    assert "parseError" in parsed.extra


def test_parse_score_is_int_branch_pct():
    parser = VitestCovParser()
    parsed = parser.parse(SAMPLE_JSON)

    branch = next(m for m in parsed.metrics if m.key == "totalBranch")
    assert parsed.score.value == int(branch.value)


# ---------------------------------------------------------------------------
# parse_file_error
# ---------------------------------------------------------------------------

def test_parse_file_error_returns_failure():
    parser = VitestCovParser()
    reading = parser.parse_file_error("coverage/coverage-final.json", "")

    assert reading.success is False
    assert "failing" in reading.summary.lower()
    assert "parseError" in reading.extra


def test_parse_file_error_summary_mentions_tests():
    parser = VitestCovParser()
    reading = parser.parse_file_error("coverage/coverage-final.json", "some stdout")

    assert "tests" in reading.summary.lower()


# ---------------------------------------------------------------------------
# is_watch_run_complete
# ---------------------------------------------------------------------------

def test_watch_complete_passed():
    parser = VitestCovParser()
    assert parser.is_watch_run_complete("      Tests  38 passed (38)") is True


def test_watch_complete_failed():
    parser = VitestCovParser()
    assert parser.is_watch_run_complete("      Tests  2 failed | 36 passed (38)") is True


def test_watch_not_complete():
    parser = VitestCovParser()
    assert parser.is_watch_run_complete("running tests...") is False


# ---------------------------------------------------------------------------
# _format_line_ranges helper
# ---------------------------------------------------------------------------

def test_format_line_ranges_empty():
    assert _format_line_ranges([]) == ""


def test_format_line_ranges_single():
    assert _format_line_ranges([5]) == "5"


def test_format_line_ranges_consecutive():
    assert _format_line_ranges([10, 11, 12]) == "10-12"


def test_format_line_ranges_mixed():
    assert _format_line_ranges([10, 11, 12, 15, 20, 21]) == "10-12,15,20-21"


def test_format_line_ranges_scattered():
    assert _format_line_ranges([1, 3, 5]) == "1,3,5"

