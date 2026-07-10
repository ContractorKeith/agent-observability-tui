from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from agent_observability_tui.sources import (
    JsonlTail,
    ProcessExited,
    ProcessOutput,
    ProcessStarted,
    SourceDiagnostic,
    SupervisedChild,
    iter_completed_jsonl,
)


def test_completed_jsonl_accepts_final_unterminated_record(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    trace.write_bytes(b'{"sequence": 1}\n{"sequence": 2}')

    items = list(iter_completed_jsonl(trace, json.loads))

    assert items == [{"sequence": 1}, {"sequence": 2}]


def test_tail_waits_for_a_complete_line_and_recovers_after_truncation(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    trace.write_bytes(b'{"sequence":')
    tail = JsonlTail(trace, json.loads)

    assert tail.poll() == []

    with trace.open("ab") as handle:
        handle.write(b" 1}\n")
    assert tail.poll() == [{"sequence": 1}]

    trace.write_bytes(b"{}\n")
    items = tail.poll()

    assert isinstance(items[0], SourceDiagnostic)
    assert items[0].code == "source_truncated"
    assert items[1:] == [{}]


def test_tail_detects_rotation_and_starts_at_new_file_beginning(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    trace.write_bytes(b'{"generation": 1}\n')
    tail = JsonlTail(trace, json.loads)
    assert tail.poll() == [{"generation": 1}]

    rotated = tmp_path / "trace.old.jsonl"
    trace.replace(rotated)
    trace.write_bytes(b'{"generation": 2}\n')

    items = tail.poll()

    assert isinstance(items[0], SourceDiagnostic)
    assert items[0].code == "source_rotated"
    assert items[1:] == [{"generation": 2}]


def test_tail_bounds_an_oversized_partial_line(tmp_path: Path) -> None:
    trace = tmp_path / "trace.jsonl"
    trace.write_bytes(b"x" * 64)
    tail = JsonlTail(trace, json.loads, max_line_bytes=16)

    items = tail.poll()

    assert len(items) == 1
    assert isinstance(items[0], SourceDiagnostic)
    assert items[0].code == "line_too_long"
    assert tail.pending_bytes == 0


def test_supervised_child_drains_stdout_and_stderr_bursts_without_deadlock(
    tmp_path: Path,
) -> None:
    line_count = 500
    script = (
        "import sys\n"
        f"for i in range({line_count}):\n"
        " print(f'out-{i}')\n"
        " print(f'err-{i}', file=sys.stderr)\n"
    )
    runner = SupervisedChild(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=os.environ,
    )

    events = list(runner.run())
    outputs = [event for event in events if isinstance(event, ProcessOutput)]

    assert isinstance(events[0], ProcessStarted)
    assert sum(event.stream == "stdout" for event in outputs) == line_count
    assert sum(event.stream == "stderr" for event in outputs) == line_count
    assert isinstance(events[-1], ProcessExited)
    assert events[-1].returncode == 0


def test_supervised_child_treats_shell_metacharacters_as_literal_arguments(
    tmp_path: Path,
) -> None:
    literal = "; printf SHOULD_NOT_RUN"
    runner = SupervisedChild(
        [sys.executable, "-c", "import sys; print(sys.argv[1])", literal],
        cwd=tmp_path,
        env=os.environ,
    )

    events = list(runner.run())
    output = [event.text for event in events if isinstance(event, ProcessOutput)]

    assert output == [literal]
    assert isinstance(events[-1], ProcessExited)
    assert events[-1].returncode == 0


def test_supervised_child_reports_fast_exit_and_bounds_output_lines(tmp_path: Path) -> None:
    runner = SupervisedChild(
        [sys.executable, "-c", "print('x' * 200)"],
        cwd=tmp_path,
        env=os.environ,
        max_line_bytes=32,
    )

    events = list(runner.run())
    output = next(event for event in events if isinstance(event, ProcessOutput))

    assert len(output.text.encode()) <= 32
    assert output.truncated is True
    assert output.byte_count == 200
    assert isinstance(events[-1], ProcessExited)
    assert events[-1].status == "exited"
    assert events[-1].returncode == 0


def test_supervised_child_cleans_up_descendants_holding_output_pipes(tmp_path: Path) -> None:
    if os.name != "posix":
        return
    script = (
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "print('parent-exiting', flush=True)\n"
    )
    runner = SupervisedChild(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=os.environ,
        termination_grace_seconds=0.2,
        drain_timeout_seconds=1.0,
    )

    started_at = time.monotonic()
    events = list(runner.run())

    assert time.monotonic() - started_at < 2.0
    assert any(
        isinstance(event, ProcessOutput) and event.text == "parent-exiting" for event in events
    )
    assert isinstance(events[-1], ProcessExited)
    assert events[-1].returncode == 0


def test_supervised_child_force_kills_detached_output_descendant(tmp_path: Path) -> None:
    if os.name != "posix":
        return
    pid_file = tmp_path / "descendant.pid"
    child = (
        "import os, pathlib, signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); "
        "time.sleep(30)"
    )
    parent = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}], "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        "time.sleep(0.1)"
    )
    runner = SupervisedChild(
        [sys.executable, "-c", parent],
        cwd=tmp_path,
        env=os.environ,
        termination_grace_seconds=0.1,
    )

    events = list(runner.run())
    descendant_pid = int(pid_file.read_text())

    assert isinstance(events[-1], ProcessExited)
    deadline = time.monotonic() + 2
    while _pid_is_running(descendant_pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not _pid_is_running(descendant_pid)


def _pid_is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        import psutil

        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except (psutil.Error, ProcessLookupError):
        return False


def test_closing_child_event_iterator_cleans_up_the_process(tmp_path: Path) -> None:
    runner = SupervisedChild(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=tmp_path,
        env=os.environ,
        termination_grace_seconds=0.2,
    )
    events = runner.run()

    assert isinstance(next(events), ProcessStarted)
    events.close()

    assert runner.pid is None
