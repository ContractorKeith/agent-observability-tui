from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil

from agent_observability_tui.cli import main


def test_demo_headless_then_verify_and_export(tmp_path: Path, capsys) -> None:
    database = tmp_path / "sessions.sqlite3"

    assert main(["demo", "--headless", "--db", str(database)]) == 0
    demo_result = json.loads(capsys.readouterr().out)
    session_id = demo_result["session_id"]

    assert main(["verify", session_id, "--json", "--db", str(database)]) == 0
    verification = json.loads(capsys.readouterr().out)
    assert verification["valid"] is True

    destination = tmp_path / "evidence.json"
    assert (
        main(
            [
                "export",
                session_id,
                str(destination),
                "--format",
                "json",
                "--db",
                str(database),
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert json.loads(destination.read_text(encoding="utf-8"))["verification"]["valid"] is True


def test_run_headless_uses_argv_and_captures_both_streams(tmp_path: Path, capsys) -> None:
    database = tmp_path / "sessions.sqlite3"
    literal = "; printf SHOULD_NOT_RUN"
    script = "import sys; print(sys.argv[1]); print('warning', file=sys.stderr)"

    exit_code = main(
        [
            "run",
            "--headless",
            "--db",
            str(database),
            "--cwd",
            str(tmp_path),
            "--",
            sys.executable,
            "-c",
            script,
            literal,
        ]
    )
    result = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert result["summary"]["status"] == "complete"
    assert result["summary"]["event_count"] >= 4
    assert "SHOULD_NOT_RUN" not in tuple(path.name for path in tmp_path.iterdir())


def test_unknown_session_is_a_clean_cli_error(tmp_path: Path, capsys) -> None:
    database = tmp_path / "db.sqlite3"
    assert main(["demo", "--headless", "--db", str(database)]) == 0
    capsys.readouterr()
    exit_code = main(["replay", "missing-session", "--headless", "--db", str(database)])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "missing-session" in captured.err
    assert "Traceback" not in captured.err


def test_secret_or_control_bearing_session_id_is_rejected_before_storage(
    tmp_path: Path, capsys
) -> None:
    database = tmp_path / "db.sqlite3"
    secret = "ghp_exampleSecretValue123456789"

    exit_code = main(
        [
            "import",
            str(Path(__file__).parents[1] / "examples" / "demo-trace.jsonl"),
            "--adapter",
            "native",
            "--session",
            secret,
            "--db",
            str(database),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert secret not in captured.err


def test_headless_ctrl_c_stops_supervised_process_tree(tmp_path: Path) -> None:
    if os.name != "posix":
        return
    pid_file = tmp_path / "child.pid"
    script = (
        "import os, pathlib, signal, time; "
        "signal.signal(signal.SIGINT, signal.SIG_IGN); "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); "
        "time.sleep(30)"
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "agent_observability_tui",
            "run",
            "--headless",
            "--db",
            str(tmp_path / "db.sqlite3"),
            "--cwd",
            str(tmp_path),
            "--",
            sys.executable,
            "-c",
            script,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 5
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert pid_file.exists()
    child_pid = int(pid_file.read_text())

    process.send_signal(signal.SIGINT)
    _, stderr = process.communicate(timeout=8)

    assert process.returncode == 130
    assert "interrupted; supervised process stopped" in stderr
    assert "Traceback" not in stderr
    deadline = time.monotonic() + 2
    while _pid_is_running(child_pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not _pid_is_running(child_pid)


def test_headless_sigterm_is_forwarded_and_escalated(tmp_path: Path) -> None:
    if os.name != "posix":
        return
    pid_file = tmp_path / "term-child.pid"
    script = (
        "import os, pathlib, signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); "
        "time.sleep(30)"
    )
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "agent_observability_tui",
            "run",
            "--headless",
            "--db",
            str(tmp_path / "term.sqlite3"),
            "--cwd",
            str(tmp_path),
            "--",
            sys.executable,
            "-c",
            script,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 5
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    child_pid = int(pid_file.read_text())

    process.send_signal(signal.SIGTERM)
    _, stderr = process.communicate(timeout=8)

    assert process.returncode == 143
    assert "signal forwarded; supervised process stopped" in stderr
    assert not _pid_is_running(child_pid)


def test_tui_quit_escalates_to_kill_for_term_ignoring_child(tmp_path: Path, monkeypatch) -> None:
    if os.name != "posix":
        return
    pid_file = tmp_path / "child.pid"
    script = (
        "import os, pathlib, signal, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); "
        "time.sleep(30)"
    )

    def wait_for_child_then_quit(_app) -> None:
        deadline = time.monotonic() + 5
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)

    monkeypatch.setattr(
        "agent_observability_tui.cli.ObservabilityApp.run", wait_for_child_then_quit
    )

    exit_code = main(
        [
            "run",
            "--db",
            str(tmp_path / "db.sqlite3"),
            "--cwd",
            str(tmp_path),
            "--",
            sys.executable,
            "-c",
            script,
        ]
    )
    child_pid = int(pid_file.read_text())

    assert exit_code == 0
    deadline = time.monotonic() + 2
    while _pid_is_running(child_pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not _pid_is_running(child_pid)


def _pid_is_running(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except (psutil.Error, ProcessLookupError):
        return False
