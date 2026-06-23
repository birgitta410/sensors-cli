"""Unit tests for GenericRunner watch-mode helpers (no real subprocesses)."""

import asyncio
import platform
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sensors.config import ScoreInfo, SensorReading
from sensors.config.schema import RunnerConfig, RunnerMode
from sensors.persistence.state_manager import StateManager
from sensors.runners.generic import GenericRunner
from sensors.runners.parsers.base import OutputParser


class _WatchParser(OutputParser):
    """Parser that completes on a fixed marker line."""

    def __init__(self, *, complete_marker: str = "RUN_COMPLETE", fail_parse: bool = False):
        self.complete_marker = complete_marker
        self.fail_parse = fail_parse
        self.parsed_outputs: list[str] = []

    def is_watch_run_complete(self, line: str) -> bool:
        return line == self.complete_marker

    def parse(self, output: str) -> SensorReading:
        if self.fail_parse:
            raise ValueError("parse failed")
        self.parsed_outputs.append(output)
        return SensorReading(
            success=True,
            summary=f"{output.count(chr(10)) + 1} lines",
            score=ScoreInfo(value=0, direction="less", description="n/a"),
        )


def _watch_config(**kwargs) -> RunnerConfig:
    defaults = {
        "name": "watcher",
        "parser": "vitest",
        "enabled": True,
        "mode": RunnerMode.WATCH,
        "command": "npm test --watch",
    }
    defaults.update(kwargs)
    return RunnerConfig(**defaults)


def _runner(
    parser: _WatchParser | None = None,
    *,
    state_manager: StateManager | None = None,
    **config_kwargs,
) -> GenericRunner:
    return GenericRunner(
        _watch_config(**config_kwargs),
        parser or _WatchParser(),
        state_manager=state_manager,
    )


@pytest.mark.parametrize(
    ("current", "expected"),
    [
        (1.0, 2.0),
        (30.0, 60.0),
        (60.0, 60.0),
    ],
)
def test_backoff_delay(current: float, expected: float) -> None:
    assert GenericRunner._backoff_delay(current) == expected


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_wrap_with_script(system: str) -> None:
    with patch.object(platform, "system", return_value=system):
        wrapped = GenericRunner._wrap_with_script("npm test")
    if system == "Darwin":
        assert wrapped == "script -q /dev/null npm test"
    else:
        assert wrapped.startswith("script -qfc ")
        assert "npm test" in wrapped


@pytest.mark.asyncio
async def test_handle_watch_line_accumulates_until_boundary() -> None:
    runner = _runner()
    acc: list[str] = []
    acc = await runner._handle_watch_line("line one", acc)
    acc = await runner._handle_watch_line("line two", acc)
    assert acc == ["line one", "line two"]


@pytest.mark.asyncio
async def test_handle_watch_line_parses_and_clears_on_boundary() -> None:
    parser = _WatchParser()
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(Path(tmpdir) / "state.json")
        runner = _runner(parser, state_manager=sm)
        acc = await runner._handle_watch_line("header", [])
        acc = await runner._handle_watch_line("RUN_COMPLETE", acc)

        assert acc == []
        assert parser.parsed_outputs == ["header\nRUN_COMPLETE"]
        state = await sm.read_state()
        assert state.runners["watcher"].status == "success"


@pytest.mark.asyncio
async def test_handle_watch_line_parse_error_clears_accumulator(capsys) -> None:
    parser = _WatchParser(fail_parse=True)
    runner = _runner(parser)
    acc = await runner._handle_watch_line("RUN_COMPLETE", ["partial"])
    assert acc == []
    out = capsys.readouterr().out
    assert "[watcher] Parse error:" in out


@pytest.mark.asyncio
async def test_spawn_watch_process_wraps_command_and_env() -> None:
    runner = _runner(workingDir="/tmp/proj")
    fake_proc = MagicMock()
    with patch(
        "sensors.runners.generic.asyncio.create_subprocess_shell",
        new_callable=AsyncMock,
        return_value=fake_proc,
    ) as spawn:
        proc = await runner._spawn_watch_process("npm test")

    assert proc is fake_proc
    spawn.assert_awaited_once()
    call_kw = spawn.call_args.kwargs
    assert call_kw["cwd"] == "/tmp/proj"
    assert call_kw["env"]["NO_COLOR"] == "1"
    assert call_kw["env"]["FORCE_COLOR"] == "0"
    cmd = spawn.call_args.args[0]
    assert "npm test" in cmd
    assert cmd.startswith("script ")


@pytest.mark.asyncio
async def test_terminate_watch_process_kills_on_timeout() -> None:
    runner = _runner()
    process = MagicMock()
    process.pid = 1234
    process.returncode = None
    process.wait = AsyncMock(side_effect=[asyncio.TimeoutError(), None, None])

    with patch("sensors.runners.generic.os.getpgid", return_value=1234) as mock_getpgid, \
         patch("sensors.runners.generic.os.killpg") as mock_killpg, \
         patch(
             "sensors.runners.generic.asyncio.wait_for",
             new_callable=AsyncMock,
             side_effect=[asyncio.TimeoutError(), None, None],
         ):
        await runner._terminate_watch_process(process)

    import signal as _signal
    assert mock_getpgid.call_count == 2
    mock_killpg.assert_any_call(1234, _signal.SIGTERM)
    mock_killpg.assert_any_call(1234, _signal.SIGKILL)


@pytest.mark.asyncio
async def test_run_watch_mode_processes_stdout_then_stops() -> None:
    parser = _WatchParser()
    runner = _runner(parser)
    runner._should_stop = False

    stdout = asyncio.StreamReader()
    stdout.feed_data(b"line a\nRUN_COMPLETE\n")
    stdout.feed_eof()

    process = MagicMock()
    process.stdout = stdout
    process.returncode = 0
    process.wait = AsyncMock(return_value=0)

    sleep_calls = 0

    async def stop_after_retry(delay: float, max_delay: float) -> float:
        nonlocal sleep_calls
        sleep_calls += 1
        runner._should_stop = True
        return delay

    with patch.object(
        runner, "_spawn_watch_process", new_callable=AsyncMock, return_value=process
    ), patch.object(runner, "_sleep_watch_retry", side_effect=stop_after_retry):
        await runner._run_watch_mode()

    assert parser.parsed_outputs == ["line a\nRUN_COMPLETE"]
    assert sleep_calls == 1
