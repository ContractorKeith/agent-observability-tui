"""Public command-line interface and composition root."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .app import ObservabilityApp
from .export import verify_export
from .observatory import Observatory, default_database_path
from .redaction import sanitize_text
from .sources import JsonlTail, ProcessExited, ProcessStarted, SupervisedChild
from .telemetry import ProcessSampler

ADAPTERS = ("auto", "native", "mcp", "hermes", "openhands")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-observe",
        description="Trace, replay, compare, and export local agent sessions.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="subcommand", required=True)

    demo = commands.add_parser("demo", help="Load a sanitized demo and open the dashboard")
    _database_option(demo)
    demo.add_argument("--headless", action="store_true", help="Print summary JSON and exit")
    demo.set_defaults(handler=_demo)

    import_command = commands.add_parser("import", help="Import a completed JSONL trace")
    import_command.add_argument("path", type=Path)
    _adapter_option(import_command)
    _database_option(import_command)
    import_command.add_argument("--session", help="Collector session ID")
    import_command.add_argument("--json", action="store_true", help="Print machine-readable result")
    import_command.set_defaults(handler=_import)

    watch = commands.add_parser("watch", help="Tail a JSONL trace in the dashboard")
    watch.add_argument("path", type=Path)
    _adapter_option(watch)
    _database_option(watch)
    watch.add_argument("--session", help="Collector session ID")
    watch.set_defaults(handler=_watch)

    run = commands.add_parser("run", help="Launch and observe one child command")
    _adapter_option(run)
    _database_option(run)
    run.add_argument("--session", help="Collector session ID")
    run.add_argument(
        "--cwd", type=Path, default=Path.cwd(), help="Explicit child working directory"
    )
    run.add_argument(
        "--clean-env",
        action="store_true",
        help="Start with only PATH/HOME/locale/temp environment variables",
    )
    run.add_argument("--drop-env", action="append", default=[], metavar="NAME")
    run.add_argument("--env", action="append", default=[], metavar="NAME=VALUE")
    run.add_argument("--headless", action="store_true", help="Run to completion and print JSON")
    run.add_argument("command", nargs=argparse.REMAINDER, help="Command after --")
    run.set_defaults(handler=_run)

    sessions = commands.add_parser("sessions", help="List stored sessions")
    _database_option(sessions)
    sessions.add_argument("--json", action="store_true")
    sessions.set_defaults(handler=_sessions)

    replay = commands.add_parser("replay", help="Open a stored session without executing it")
    replay.add_argument("session")
    _database_option(replay)
    replay.add_argument("--headless", action="store_true", help="Print summary JSON and exit")
    replay.set_defaults(handler=_replay)

    compare = commands.add_parser("compare", help="Compare already captured sessions read-only")
    compare.add_argument("sessions", nargs="+")
    _database_option(compare)
    compare.add_argument("--json", action="store_true")
    compare.set_defaults(handler=_compare)

    export = commands.add_parser("export", help="Export sanitized evidence")
    export.add_argument("session")
    export.add_argument("destination", type=Path)
    _database_option(export)
    export.add_argument("--format", choices=("json", "markdown"), default="json")
    export.set_defaults(handler=_export)

    verify = commands.add_parser("verify", help="Verify the sanitized record chain")
    verify.add_argument("session")
    _database_option(verify)
    verify.add_argument("--json", action="store_true")
    verify.set_defaults(handler=_verify)

    verify_export_command = commands.add_parser(
        "verify-export", help="Verify a standalone JSON evidence export"
    )
    verify_export_command.add_argument("path", type=Path)
    verify_export_command.add_argument("--json", action="store_true")
    verify_export_command.set_defaults(handler=_verify_export)
    return parser


def _database_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", type=Path, default=default_database_path())
    parser.add_argument(
        "--prices",
        type=Path,
        help="Versioned TOML price catalog (or AGENT_OBSERVABILITY_PRICES)",
    )


def _adapter_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--adapter", choices=ADAPTERS, default="auto")


def _observatory(args: argparse.Namespace, *, read_only: bool = False) -> Observatory:
    return Observatory.open(
        args.db,
        price_catalog=args.prices,
        read_only=read_only,
    )


def _demo(args: argparse.Namespace) -> int:
    observatory = _observatory(args)
    trace = Path(__file__).with_name("demo-trace.jsonl")
    session_id = observatory.import_path(trace, adapter="native")
    if args.headless:
        _print_json(
            {"session_id": session_id, "summary": observatory.summarize(session_id).to_dict()}
        )
        return 0
    ObservabilityApp(observatory, session_id=session_id).run()
    return 0


def _import(args: argparse.Namespace) -> int:
    observatory = _observatory(args)
    session_id = observatory.import_path(
        args.path,
        adapter=args.adapter,
        session_id=args.session,
    )
    result = {
        "session_id": session_id,
        "summary": observatory.summarize(session_id).to_dict(),
        "verification": asdict(observatory.verify(session_id)),
    }
    if args.json:
        _print_json(result)
    else:
        print(
            f"Imported {result['summary']['event_count']} sanitized events as {session_id}; "
            f"chain valid={result['verification']['valid']}."
        )
    return 0


def _watch(args: argparse.Namespace) -> int:
    observatory = _observatory(args)
    session_id = observatory.new_session(adapter=args.adapter, session_id=args.session)
    stop = threading.Event()
    tail = JsonlTail(args.path, lambda line: line)

    def worker() -> None:
        while not stop.is_set():
            for item in tail.poll():
                observatory.ingest_source_item(item, session_id=session_id, adapter=args.adapter)
            stop.wait(0.2)

    thread = threading.Thread(target=worker, name="agent-observe-watch", daemon=True)
    thread.start()
    try:
        ObservabilityApp(observatory, session_id=session_id).run()
    finally:
        stop.set()
        thread.join(timeout=2)
        observatory.store.finalize_session(session_id, status="stopped")
    return 0


def _run(args: argparse.Namespace) -> int:
    argv = list(args.command)
    if argv and argv[0] == "--":
        argv.pop(0)
    if not argv:
        raise ValueError("run requires a command after --")
    cwd = args.cwd.expanduser().resolve()
    if not cwd.is_dir():
        raise ValueError(f"child cwd is not a directory: {cwd}")
    env = _child_environment(args)
    observatory = _observatory(args)
    session_id = observatory.new_session(adapter=args.adapter, session_id=args.session)
    runner = SupervisedChild(argv, cwd=cwd, env=env)
    finished = threading.Event()
    sample_stop = threading.Event()
    process_succeeded = False

    def sample_worker(sampler: ProcessSampler) -> None:
        while not sample_stop.is_set():
            observatory.record_resource(session_id, sampler.sample())
            sample_stop.wait(0.5)

    def process_worker() -> None:
        nonlocal process_succeeded
        sampler_thread: threading.Thread | None = None
        try:
            for item in runner.run():
                observatory.ingest_source_item(item, session_id=session_id, adapter=args.adapter)
                if isinstance(item, ProcessStarted) and sampler_thread is None:
                    sampler = ProcessSampler(
                        item.pid, expected_create_time=item.process_create_time
                    )
                    sampler_thread = threading.Thread(
                        target=sample_worker,
                        args=(sampler,),
                        name="agent-observe-sampler",
                        daemon=True,
                    )
                    sampler_thread.start()
                if isinstance(item, ProcessExited):
                    process_succeeded = item.returncode == 0 and item.status == "exited"
                    sample_stop.set()
        finally:
            sample_stop.set()
            if sampler_thread is not None:
                sampler_thread.join(timeout=1)
            try:
                info = observatory.store.get_session(session_id)
                if not info.finalized:
                    final_status = "complete" if process_succeeded else "error"
                    observatory.store.finalize_session(session_id, status=final_status)
            finally:
                finished.set()

    thread = threading.Thread(target=process_worker, name="agent-observe-run", daemon=True)
    thread.start()
    if args.headless:
        with _forward_signals(runner, interrupt_main=False) as forwarded:
            kill_sent = False
            try:
                while not finished.wait(timeout=0.2):
                    received_at = forwarded["received_at"]
                    if (
                        received_at is not None
                        and time.monotonic() - received_at >= 2
                        and not kill_sent
                    ):
                        runner.forward_signal(signal.SIGKILL)
                        kill_sent = True
            except KeyboardInterrupt:
                runner.forward_signal(signal.SIGINT)
                if not finished.wait(timeout=1):
                    runner.forward_signal(signal.SIGTERM)
                if not finished.wait(timeout=1):
                    runner.forward_signal(signal.SIGKILL)
                if not finished.wait(timeout=2):
                    print(
                        "agent-observe: supervised process cleanup did not finish",
                        file=sys.stderr,
                    )
                    return 1
                thread.join(timeout=0.2)
                print("agent-observe: interrupted; supervised process stopped", file=sys.stderr)
                return 130
            if forwarded["signal"] is not None:
                print(
                    "agent-observe: signal forwarded; supervised process stopped", file=sys.stderr
                )
                return 128 + int(forwarded["signal"])
            _print_json(
                {"session_id": session_id, "summary": observatory.summarize(session_id).to_dict()}
            )
            return 0 if process_succeeded else 1
    interrupted = False
    with _forward_signals(runner, interrupt_main=True) as forwarded:
        try:
            ObservabilityApp(observatory, session_id=session_id).run()
        except KeyboardInterrupt:
            interrupted = True
        finally:
            if not finished.is_set():
                runner.forward_signal(signal.SIGTERM)
            thread.join(timeout=2)
            if thread.is_alive():
                runner.forward_signal(signal.SIGKILL)
                thread.join(timeout=2)
    if thread.is_alive():
        print("agent-observe: supervised process cleanup did not finish", file=sys.stderr)
        return 1
    if forwarded["signal"] is not None:
        return 128 + int(forwarded["signal"])
    if interrupted:
        return 130
    return 0


def _sessions(args: argparse.Namespace) -> int:
    if not args.db.expanduser().is_file():
        if args.json:
            _print_json([])
        else:
            print("No stored sessions.")
        return 0
    sessions = _observatory(args, read_only=True).sessions()
    if args.json:
        _print_json([asdict(item) for item in sessions])
        return 0
    if not sessions:
        print("No stored sessions.")
        return 0
    for item in sessions:
        print(
            f"{item.session_id}\t{item.status}\t{item.event_count} events\t"
            f"{item.adapter}\tfinalized={item.finalized}"
        )
    return 0


def _replay(args: argparse.Namespace) -> int:
    observatory = _observatory(args, read_only=True)
    summary = observatory.summarize(args.session)
    if args.headless:
        _print_json(summary.to_dict())
        return 0
    ObservabilityApp(observatory, session_id=args.session).run()
    return 0


def _compare(args: argparse.Namespace) -> int:
    summaries = _observatory(args, read_only=True).compare(args.sessions)
    if args.json:
        _print_json([item.to_dict() for item in summaries])
        return 0
    for item in summaries:
        cost = "unpriced" if item.estimated_cost_usd is None else f"${item.estimated_cost_usd:.6f}"
        rss = "unknown" if item.peak_rss_bytes is None else str(item.peak_rss_bytes)
        cpu = "unknown" if item.peak_cpu_percent is None else f"{item.peak_cpu_percent:.1f}%"
        print(
            f"{item.session_id}\tmodel={item.model or 'unknown'}\tstatus={item.status}\t"
            f"events={item.event_count}\ttools={item.tool_calls}\terrors={item.errors}\t"
            f"tokens={_known(item.input_tokens)}/{_known(item.output_tokens)}\t{cost}\t"
            f"rss={rss}\tcpu={cpu}\tresources={item.resource_status}"
        )
    return 0


def _export(args: argparse.Namespace) -> int:
    destination = _observatory(args, read_only=True).export(
        args.session, args.destination, format=args.format
    )
    print(_safe_human(destination))
    return 0


def _verify(args: argparse.Namespace) -> int:
    verification = _observatory(args, read_only=True).verify(args.session)
    if args.json:
        _print_json(asdict(verification))
    else:
        print(
            f"{verification.session_id}: valid={verification.valid} "
            f"finalized={verification.finalized} events={verification.event_count}"
        )
        if verification.error:
            print(f"error: {verification.error}")
    return 0 if verification.valid and verification.finalized else 1


def _verify_export(args: argparse.Namespace) -> int:
    verification = verify_export(args.path)
    if args.json:
        _print_json(asdict(verification))
    else:
        print(
            f"{_safe_human(verification.path)}: valid={verification.valid} "
            f"finalized={verification.finalized} events={verification.event_count}"
        )
        if verification.error:
            print(f"error: {verification.error}")
    return 0 if verification.valid and verification.finalized else 1


def _child_environment(args: argparse.Namespace) -> dict[str, str]:
    if args.clean_env:
        allowed = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP", "SYSTEMROOT")
        env = {name: os.environ[name] for name in allowed if name in os.environ}
    else:
        env = dict(os.environ)
    for name in args.drop_env:
        env.pop(name, None)
    for assignment in args.env:
        if "=" not in assignment:
            raise ValueError(f"--env requires NAME=VALUE, got {assignment!r}")
        name, value = assignment.split("=", 1)
        if not name or "\x00" in name or "=" in name or "\x00" in value:
            raise ValueError("environment names/values must be non-empty and contain no NUL")
        env[name] = value
    return env


@contextmanager
def _forward_signals(runner: SupervisedChild, *, interrupt_main: bool):
    state: dict[str, int | float | None] = {"signal": None, "received_at": None}
    previous: dict[int, object] = {}

    def handler(signal_number: int, _frame) -> None:
        if state["signal"] is None:
            state["signal"] = signal_number
            state["received_at"] = time.monotonic()
        runner.forward_signal(signal_number)
        if interrupt_main:
            raise KeyboardInterrupt

    names = ("SIGTERM", "SIGHUP", "SIGQUIT")
    if threading.current_thread() is threading.main_thread():
        for name in names:
            signal_number = getattr(signal, name, None)
            if signal_number is None:
                continue
            previous[signal_number] = signal.getsignal(signal_number)
            signal.signal(signal_number, handler)
    try:
        yield state
    finally:
        for signal_number, old_handler in previous.items():
            signal.signal(signal_number, old_handler)


def _print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))


def _known(value: object | None) -> str:
    return "unknown" if value is None else str(value)


def _safe_human(value: object) -> str:
    return str(sanitize_text(str(value), max_bytes=4096).value)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (FileNotFoundError, KeyError, OSError, ValueError) as error:
        safe = sanitize_text(str(error), max_bytes=1024).value
        print(f"agent-observe: {safe}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
