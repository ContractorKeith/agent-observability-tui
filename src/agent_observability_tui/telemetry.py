"""Portable, claim-bounded process telemetry.

Only per-process RSS and CPU observations are reported.  This module intentionally
contains no GPU or Apple unified-memory attribution.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal, Protocol

import psutil


class _MemoryInfo(Protocol):
    rss: int


class ProcessLike(Protocol):
    def create_time(self) -> float: ...

    def memory_info(self) -> _MemoryInfo: ...

    def cpu_percent(self, interval: None = None) -> float: ...


ProcessFactory = Callable[[int], ProcessLike]


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class ResourceSample:
    """Observed per-process telemetry or an explicit unavailable state."""

    pid: int
    process_create_time: float | None
    observed_at: datetime = field(default_factory=_now)
    rss_bytes: int | None = None
    cpu_percent: float | None = None
    status: Literal["observed", "partial", "unavailable"] = "unavailable"
    errors: tuple[str, ...] = ()
    provenance: Literal["psutil-observed"] = "psutil-observed"


class ProcessSampler:
    """Sample one process while defending against PID reuse."""

    def __init__(
        self,
        pid: int,
        *,
        expected_create_time: float | None = None,
        process_factory: ProcessFactory = psutil.Process,
    ) -> None:
        if pid <= 0:
            raise ValueError("pid must be positive")
        self.pid = pid
        self._process_factory = process_factory
        self._identity_error: str | None = None
        if expected_create_time is not None:
            self.expected_create_time = float(expected_create_time)
        else:
            try:
                process = self._process_factory(pid)
                self.expected_create_time = float(process.create_time())
            except Exception as error:
                self.expected_create_time = None
                self._identity_error = f"identity_unavailable:{type(error).__name__}"

    def sample(self) -> ResourceSample:
        """Return a sample; OS/psutil failures are represented, never raised."""

        observed_at = _now()
        if self.expected_create_time is None:
            return ResourceSample(
                pid=self.pid,
                process_create_time=None,
                observed_at=observed_at,
                errors=(self._identity_error or "identity_unavailable",),
            )

        try:
            process = self._process_factory(self.pid)
            actual_create_time = float(process.create_time())
        except Exception as error:
            return ResourceSample(
                pid=self.pid,
                process_create_time=self.expected_create_time,
                observed_at=observed_at,
                errors=(f"identity_unavailable:{type(error).__name__}",),
            )

        if not math.isclose(
            actual_create_time,
            self.expected_create_time,
            rel_tol=0.0,
            abs_tol=1e-6,
        ):
            return ResourceSample(
                pid=self.pid,
                process_create_time=actual_create_time,
                observed_at=observed_at,
                errors=("pid_reused",),
            )

        errors: list[str] = []
        rss_bytes: int | None = None
        cpu_percent: float | None = None
        try:
            rss_bytes = int(process.memory_info().rss)
            if rss_bytes < 0:
                raise ValueError("negative RSS")
        except Exception as error:
            errors.append(f"rss_unavailable:{type(error).__name__}")
            rss_bytes = None
        try:
            measured_cpu = float(process.cpu_percent(interval=None))
            if not math.isfinite(measured_cpu) or measured_cpu < 0:
                raise ValueError("invalid CPU percentage")
            cpu_percent = measured_cpu
        except Exception as error:
            errors.append(f"cpu_unavailable:{type(error).__name__}")
            cpu_percent = None

        observed_count = int(rss_bytes is not None) + int(cpu_percent is not None)
        status: Literal["observed", "partial", "unavailable"]
        if observed_count == 2:
            status = "observed"
        elif observed_count == 1:
            status = "partial"
        else:
            status = "unavailable"
        return ResourceSample(
            pid=self.pid,
            process_create_time=actual_create_time,
            observed_at=observed_at,
            rss_bytes=rss_bytes,
            cpu_percent=cpu_percent,
            status=status,
            errors=tuple(errors),
        )


def sample_process(
    pid: int,
    *,
    expected_create_time: float | None = None,
) -> ResourceSample:
    """Create a sampler and take one identity-checked observation."""

    return ProcessSampler(pid, expected_create_time=expected_create_time).sample()


__all__ = ["ProcessFactory", "ProcessLike", "ProcessSampler", "ResourceSample", "sample_process"]
