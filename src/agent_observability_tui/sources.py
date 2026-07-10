"""Lifecycle-safe file and child-process sources.

This module deliberately knows nothing about the canonical event model.  Callers inject
a small line parser (for example, one that delegates to ``adapters.parse_record``), then
persist the returned values before presenting them.  Source failures are values rather
than silent gaps.
"""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import BinaryIO, Generic, Literal, Protocol, TypeVar

DEFAULT_MAX_LINE_BYTES = 256 * 1024
DEFAULT_MAX_POLL_BYTES = 1024 * 1024

T = TypeVar("T")
StreamName = Literal["stdout", "stderr"]


def _now() -> datetime:
    return datetime.now(UTC)


class LineParser(Protocol[T]):
    """Convert one complete UTF-8 JSONL record into an application value."""

    def __call__(self, line: str, /) -> T: ...


@dataclass(frozen=True, slots=True)
class SourceDiagnostic:
    """A visible, non-secret-bearing source lifecycle diagnostic."""

    code: str
    message: str
    source: str
    observed_at: datetime = field(default_factory=_now)
    path: str | None = None
    line_number: int | None = None
    byte_offset: int | None = None
    details: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))


@dataclass(frozen=True, slots=True)
class ProcessStarted:
    """The supervised process has successfully started."""

    pid: int
    process_create_time: float | None
    argv: tuple[str, ...]
    cwd: str
    observed_at: datetime = field(default_factory=_now)


@dataclass(frozen=True, slots=True)
class ProcessOutput:
    """One bounded child output line with its original byte count."""

    pid: int
    stream: StreamName
    text: str
    byte_count: int
    truncated: bool
    observed_at: datetime = field(default_factory=_now)

    @property
    def omitted_bytes(self) -> int:
        """Return a conservative count of raw payload bytes omitted from ``text``."""

        if not self.truncated:
            return 0
        return max(0, self.byte_count - len(self.text.encode("utf-8", errors="replace")))


@dataclass(frozen=True, slots=True)
class ProcessExited:
    """Terminal event for a launch attempt or supervised child process."""

    pid: int | None
    returncode: int | None
    status: Literal["exited", "signaled", "launch_failed"]
    duration_seconds: float
    signal_number: int | None = None
    observed_at: datetime = field(default_factory=_now)


def _diagnostic(
    code: str,
    message: str,
    *,
    source: str,
    path: Path | None = None,
    line_number: int | None = None,
    byte_offset: int | None = None,
    details: Mapping[str, object] | None = None,
) -> SourceDiagnostic:
    return SourceDiagnostic(
        code=code,
        message=message,
        source=source,
        path=str(path) if path is not None else None,
        line_number=line_number,
        byte_offset=byte_offset,
        details=details or {},
    )


def _parse_line(
    raw: bytes,
    parser: LineParser[T],
    *,
    source: str,
    path: Path,
    line_number: int,
    byte_offset: int,
) -> T | SourceDiagnostic:
    if raw.endswith(b"\r"):
        raw = raw[:-1]
    try:
        line = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return _diagnostic(
            "invalid_utf8",
            "JSONL record is not valid UTF-8",
            source=source,
            path=path,
            line_number=line_number,
            byte_offset=byte_offset,
        )
    try:
        return parser(line)
    except Exception as error:
        # Parser messages may echo hostile input or secrets, so only expose its type.
        return _diagnostic(
            "parse_error",
            "JSONL parser rejected a complete record",
            source=source,
            path=path,
            line_number=line_number,
            byte_offset=byte_offset,
            details={"error_type": type(error).__name__},
        )


def iter_completed_jsonl(
    path: str | os.PathLike[str],
    parser: LineParser[T],
    *,
    max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
) -> Iterator[T | SourceDiagnostic]:
    """Parse all records in a completed JSONL file without unbounded line reads.

    A final record does not need a trailing newline because the file is declared complete.
    Oversized records are drained and replaced by a diagnostic so the following record can
    still be processed.
    """

    if max_line_bytes < 1:
        raise ValueError("max_line_bytes must be positive")
    source_path = Path(path)
    with source_path.open("rb") as handle:
        line_number = 0
        while True:
            byte_offset = handle.tell()
            raw = handle.readline(max_line_bytes + 2)
            if not raw:
                break
            line_number += 1
            complete = raw.endswith(b"\n")
            payload = raw[:-1] if complete else raw
            if complete and payload.endswith(b"\r"):
                payload = payload[:-1]

            if len(payload) > max_line_bytes:
                byte_count = len(payload)
                while not complete:
                    remainder = handle.readline(64 * 1024)
                    if not remainder:
                        break
                    complete = remainder.endswith(b"\n")
                    byte_count += len(remainder) - (1 if complete else 0)
                yield _diagnostic(
                    "line_too_long",
                    "JSONL record exceeded the configured byte limit",
                    source="import",
                    path=source_path,
                    line_number=line_number,
                    byte_offset=byte_offset,
                    details={"byte_count": byte_count, "max_line_bytes": max_line_bytes},
                )
                continue

            yield _parse_line(
                payload,
                parser,
                source="import",
                path=source_path,
                line_number=line_number,
                byte_offset=byte_offset,
            )


class JsonlTail(Generic[T]):
    """Poll a JSONL path while retaining, but never emitting, an incomplete line."""

    def __init__(
        self,
        path: str | os.PathLike[str],
        parser: LineParser[T],
        *,
        max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
        max_poll_bytes: int = DEFAULT_MAX_POLL_BYTES,
    ) -> None:
        if max_line_bytes < 1:
            raise ValueError("max_line_bytes must be positive")
        if max_poll_bytes < 1:
            raise ValueError("max_poll_bytes must be positive")
        self.path = Path(path)
        self.parser = parser
        self.max_line_bytes = max_line_bytes
        self.max_poll_bytes = max_poll_bytes
        self._identity: tuple[int, int] | None = None
        self._offset = 0
        self._line_number = 0
        self._pending = bytearray()
        self._pending_start = 0
        self._discarding_oversize = False
        self._unavailable = False

    @property
    def offset(self) -> int:
        """Number of bytes read from the current file generation."""

        return self._offset

    @property
    def pending_bytes(self) -> int:
        """Number of bounded bytes retained for an incomplete record."""

        return len(self._pending)

    def _reset_generation(self) -> None:
        self._offset = 0
        self._line_number = 0
        self._pending.clear()
        self._pending_start = 0
        self._discarding_oversize = False

    def poll(self) -> list[T | SourceDiagnostic]:
        """Read at most ``max_poll_bytes`` and return complete records/diagnostics."""

        items: list[T | SourceDiagnostic] = []
        try:
            handle = self.path.open("rb")
        except OSError as error:
            if not self._unavailable:
                items.append(
                    _diagnostic(
                        "source_unavailable",
                        "Watched JSONL source is unavailable",
                        source="watch",
                        path=self.path,
                        details={"error_type": type(error).__name__},
                    )
                )
            self._unavailable = True
            return items

        with handle:
            stat = os.fstat(handle.fileno())
            identity = (stat.st_dev, stat.st_ino)
            if self._unavailable:
                items.append(
                    _diagnostic(
                        "source_restored",
                        "Watched JSONL source is available again",
                        source="watch",
                        path=self.path,
                    )
                )
                self._unavailable = False

            if self._identity is None:
                self._identity = identity
            elif self._identity != identity:
                items.append(
                    _diagnostic(
                        "source_rotated",
                        "Watched JSONL source was replaced; reading the new file from byte zero",
                        source="watch",
                        path=self.path,
                        details={"previous_offset": self._offset},
                    )
                )
                self._identity = identity
                self._reset_generation()
            elif stat.st_size < self._offset:
                items.append(
                    _diagnostic(
                        "source_truncated",
                        "Watched JSONL source shrank; reading it again from byte zero",
                        source="watch",
                        path=self.path,
                        details={"previous_offset": self._offset, "new_size": stat.st_size},
                    )
                )
                self._reset_generation()

            try:
                handle.seek(self._offset)
                remaining = self.max_poll_bytes
                while remaining:
                    chunk = handle.read(min(64 * 1024, remaining))
                    if not chunk:
                        break
                    chunk_start = self._offset
                    self._offset += len(chunk)
                    remaining -= len(chunk)
                    self._consume_chunk(chunk, chunk_start, items)
            except OSError as error:
                items.append(
                    _diagnostic(
                        "source_read_error",
                        "Watched JSONL source could not be read",
                        source="watch",
                        path=self.path,
                        byte_offset=self._offset,
                        details={"error_type": type(error).__name__},
                    )
                )
        return items

    def _consume_chunk(
        self,
        chunk: bytes,
        chunk_start: int,
        items: list[T | SourceDiagnostic],
    ) -> None:
        cursor = 0
        while cursor < len(chunk):
            newline = chunk.find(b"\n", cursor)
            end = len(chunk) if newline == -1 else newline
            segment = chunk[cursor:end]
            if not self._discarding_oversize:
                if not self._pending:
                    self._pending_start = chunk_start + cursor
                allowed = self.max_line_bytes - len(self._pending)
                if len(segment) <= allowed:
                    self._pending.extend(segment)
                else:
                    if allowed > 0:
                        self._pending.extend(segment[:allowed])
                    items.append(
                        _diagnostic(
                            "line_too_long",
                            "JSONL record exceeded the configured byte limit",
                            source="watch",
                            path=self.path,
                            line_number=self._line_number + 1,
                            byte_offset=self._pending_start,
                            details={"max_line_bytes": self.max_line_bytes},
                        )
                    )
                    self._pending.clear()
                    self._discarding_oversize = True

            if newline == -1:
                break

            self._line_number += 1
            if self._discarding_oversize:
                self._discarding_oversize = False
            else:
                items.append(
                    _parse_line(
                        bytes(self._pending),
                        self.parser,
                        source="watch",
                        path=self.path,
                        line_number=self._line_number,
                        byte_offset=self._pending_start,
                    )
                )
                self._pending.clear()
            cursor = newline + 1
            self._pending_start = chunk_start + cursor


@dataclass(frozen=True, slots=True)
class _StreamDone:
    stream: StreamName


class _BoundedLineAccumulator:
    def __init__(self, max_line_bytes: int) -> None:
        self.max_line_bytes = max_line_bytes
        self.kept = bytearray()
        self.byte_count = 0

    def feed(self, chunk: bytes) -> Iterator[tuple[bytes, int, bool]]:
        cursor = 0
        while cursor < len(chunk):
            newline = chunk.find(b"\n", cursor)
            end = len(chunk) if newline == -1 else newline
            segment = chunk[cursor:end]
            self.byte_count += len(segment)
            allowed = self.max_line_bytes - len(self.kept)
            if allowed > 0:
                self.kept.extend(segment[:allowed])
            if newline == -1:
                break
            yield self._finish()
            cursor = newline + 1

    def finish_eof(self) -> tuple[bytes, int, bool] | None:
        if self.byte_count == 0 and not self.kept:
            return None
        return self._finish()

    def _finish(self) -> tuple[bytes, int, bool]:
        kept = bytes(self.kept)
        if kept.endswith(b"\r"):
            kept = kept[:-1]
        byte_count = self.byte_count
        truncated = byte_count > self.max_line_bytes
        self.kept.clear()
        self.byte_count = 0
        return kept, byte_count, truncated


class SupervisedChild:
    """Run exactly one argv-based child and drain both output streams concurrently."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        cwd: str | os.PathLike[str],
        env: Mapping[str, str],
        max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
        termination_grace_seconds: float = 1.0,
        drain_timeout_seconds: float = 2.0,
    ) -> None:
        if not argv or not all(isinstance(argument, str) for argument in argv):
            raise ValueError("argv must be a non-empty sequence of strings")
        if max_line_bytes < 1:
            raise ValueError("max_line_bytes must be positive")
        if termination_grace_seconds < 0 or drain_timeout_seconds <= 0:
            raise ValueError("process timeouts must be positive")
        self.argv = tuple(argv)
        self.cwd = Path(cwd)
        self.env = dict(env)
        self.max_line_bytes = max_line_bytes
        self.termination_grace_seconds = termination_grace_seconds
        self.drain_timeout_seconds = drain_timeout_seconds
        self._process: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()

    @property
    def pid(self) -> int | None:
        with self._lock:
            return self._process.pid if self._process is not None else None

    def forward_signal(self, signal_number: int) -> bool:
        """Forward a signal to the supervised process group, if it is running."""

        with self._lock:
            process = self._process
        if process is None or process.poll() is not None:
            return False
        self._signal_group(process, signal_number)
        return True

    def run(
        self,
    ) -> Iterator[ProcessStarted | ProcessOutput | SourceDiagnostic | ProcessExited]:
        """Yield child lifecycle events, with the terminal exit event last."""

        started_at = time.monotonic()
        process: subprocess.Popen[bytes] | None = None
        threads: list[threading.Thread] = []
        output_queue: queue.Queue[ProcessOutput | SourceDiagnostic | _StreamDone] = queue.Queue(
            maxsize=1024
        )
        try:
            process = self._spawn()
        except OSError as error:
            yield _diagnostic(
                "child_launch_failed",
                "Child process could not be launched",
                source="run",
                path=self.cwd,
                details={"error_type": type(error).__name__},
            )
            yield ProcessExited(
                pid=None,
                returncode=None,
                status="launch_failed",
                duration_seconds=time.monotonic() - started_at,
            )
            return

        with self._lock:
            self._process = process
        done_streams: set[StreamName] = set()
        returncode: int | None = None
        exited_at: float | None = None
        descendants_terminated = False
        descendants_killed = False
        drain_timed_out = False
        try:
            assert process.stdout is not None
            assert process.stderr is not None
            for name, pipe in (("stdout", process.stdout), ("stderr", process.stderr)):
                thread = threading.Thread(
                    target=self._drain_pipe,
                    args=(process.pid, name, pipe, output_queue),
                    name=f"agent-observe-{process.pid}-{name}",
                    daemon=True,
                )
                thread.start()
                threads.append(thread)

            yield ProcessStarted(
                pid=process.pid,
                process_create_time=self._get_process_create_time(process.pid),
                argv=self.argv,
                cwd=str(self.cwd),
            )

            while returncode is None or len(done_streams) < 2:
                returncode = process.poll()
                now = time.monotonic()
                if returncode is not None and exited_at is None:
                    exited_at = now

                if returncode is not None and len(done_streams) < 2 and exited_at is not None:
                    elapsed = now - exited_at
                    if elapsed >= 0.05 and not descendants_terminated:
                        self._signal_group(process, signal.SIGTERM)
                        descendants_terminated = True
                    if elapsed >= self.termination_grace_seconds and not descendants_killed:
                        self._signal_group(process, signal.SIGKILL)
                        descendants_killed = True
                    if elapsed >= self.drain_timeout_seconds:
                        drain_timed_out = True
                        break

                try:
                    item = output_queue.get(timeout=0.02)
                except queue.Empty:
                    continue
                if isinstance(item, _StreamDone):
                    done_streams.add(item.stream)
                else:
                    yield item

            if returncode is None:
                returncode = process.wait(timeout=self.termination_grace_seconds)

            while True:
                try:
                    item = output_queue.get_nowait()
                except queue.Empty:
                    break
                if isinstance(item, _StreamDone):
                    done_streams.add(item.stream)
                else:
                    yield item

            if drain_timed_out:
                yield _diagnostic(
                    "stream_drain_timeout",
                    "Child output streams did not close before the drain deadline",
                    source="run",
                    details={"pid": process.pid},
                )
        except KeyboardInterrupt:
            self._signal_group(process, signal.SIGINT)
            raise
        finally:
            self._cleanup_process(process)
            for pipe in (process.stdout, process.stderr):
                with suppress(OSError):
                    pipe.close()
            for thread in threads:
                thread.join(timeout=0.2)
            with self._lock:
                if self._process is process:
                    self._process = None

        assert returncode is not None
        status: Literal["exited", "signaled"] = "signaled" if returncode < 0 else "exited"
        yield ProcessExited(
            pid=process.pid,
            returncode=returncode,
            status=status,
            signal_number=-returncode if returncode < 0 else None,
            duration_seconds=time.monotonic() - started_at,
        )

    def _spawn(self) -> subprocess.Popen[bytes]:
        options: dict[str, object] = {}
        if os.name == "posix":
            options["start_new_session"] = True
        elif os.name == "nt":  # pragma: no cover - exercised on Windows CI only
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        return subprocess.Popen(
            self.argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=self.cwd,
            env=self.env,
            shell=False,
            bufsize=0,
            close_fds=True,
            **options,
        )

    def _drain_pipe(
        self,
        pid: int,
        stream: StreamName,
        pipe: BinaryIO,
        output_queue: queue.Queue[ProcessOutput | SourceDiagnostic | _StreamDone],
    ) -> None:
        accumulator = _BoundedLineAccumulator(self.max_line_bytes)
        try:
            while True:
                chunk = os.read(pipe.fileno(), 64 * 1024)
                if not chunk:
                    break
                for raw, byte_count, truncated in accumulator.feed(chunk):
                    output_queue.put(
                        ProcessOutput(
                            pid=pid,
                            stream=stream,
                            text=raw.decode("utf-8", errors="replace"),
                            byte_count=byte_count,
                            truncated=truncated,
                        )
                    )
            final = accumulator.finish_eof()
            if final is not None:
                raw, byte_count, truncated = final
                output_queue.put(
                    ProcessOutput(
                        pid=pid,
                        stream=stream,
                        text=raw.decode("utf-8", errors="replace"),
                        byte_count=byte_count,
                        truncated=truncated,
                    )
                )
        except OSError as error:
            output_queue.put(
                _diagnostic(
                    "stream_read_error",
                    "A child output stream could not be read",
                    source="run",
                    details={"pid": pid, "stream": stream, "error_type": type(error).__name__},
                )
            )
        finally:
            output_queue.put(_StreamDone(stream))

    def _cleanup_process(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is None:
            self._signal_group(process, signal.SIGTERM)
            try:
                process.wait(timeout=self.termination_grace_seconds)
            except subprocess.TimeoutExpired:
                self._signal_group(process, signal.SIGKILL)
                try:
                    process.wait(timeout=max(self.termination_grace_seconds, 0.1))
                except subprocess.TimeoutExpired:
                    return
        if os.name == "posix":
            # The direct child may exit after leaving descendants that closed/redirected its
            # pipes. Give the dedicated process group a grace window, then force it down.
            self._signal_group(process, signal.SIGTERM)
            deadline = time.monotonic() + self.termination_grace_seconds
            while self._process_group_exists(process.pid) and time.monotonic() < deadline:
                time.sleep(0.02)
            if self._process_group_exists(process.pid):
                self._signal_group(process, signal.SIGKILL)

    @staticmethod
    def _signal_group(process: subprocess.Popen[bytes], signal_number: int) -> None:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal_number)
            elif process.poll() is None:  # pragma: no cover - Windows only
                if signal_number == signal.SIGKILL:
                    process.kill()
                else:
                    process.send_signal(signal_number)
        except (ProcessLookupError, PermissionError, OSError):
            return

    @staticmethod
    def _process_group_exists(process_group_id: int) -> bool:
        if os.name != "posix":  # pragma: no cover - Windows only
            return False
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return False
        except (PermissionError, OSError):
            return True
        return True

    @staticmethod
    def _get_process_create_time(pid: int) -> float | None:
        try:
            import psutil

            return float(psutil.Process(pid).create_time())
        except Exception:
            return None


def run_child(
    argv: Sequence[str],
    *,
    cwd: str | os.PathLike[str],
    env: Mapping[str, str],
    max_line_bytes: int = DEFAULT_MAX_LINE_BYTES,
) -> Iterator[ProcessStarted | ProcessOutput | SourceDiagnostic | ProcessExited]:
    """Convenience wrapper around :class:`SupervisedChild`."""

    return SupervisedChild(
        argv,
        cwd=cwd,
        env=env,
        max_line_bytes=max_line_bytes,
    ).run()


__all__ = [
    "DEFAULT_MAX_LINE_BYTES",
    "DEFAULT_MAX_POLL_BYTES",
    "JsonlTail",
    "LineParser",
    "ProcessExited",
    "ProcessOutput",
    "ProcessStarted",
    "SourceDiagnostic",
    "SupervisedChild",
    "iter_completed_jsonl",
    "run_child",
]
