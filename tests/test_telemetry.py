from __future__ import annotations

from dataclasses import dataclass

from agent_observability_tui.telemetry import ProcessSampler


@dataclass
class _MemoryInfo:
    rss: int


class _ObservedProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid

    def create_time(self) -> float:
        return 100.25

    def memory_info(self) -> _MemoryInfo:
        return _MemoryInfo(rss=8_192)

    def cpu_percent(self, interval: None = None) -> float:
        assert interval is None
        return 12.5


def test_sampler_reports_observed_process_metrics() -> None:
    sampler = ProcessSampler(42, process_factory=_ObservedProcess)

    sample = sampler.sample()

    assert sample.status == "observed"
    assert sample.rss_bytes == 8_192
    assert sample.cpu_percent == 12.5
    assert sample.process_create_time == 100.25
    assert sample.errors == ()


class _ReusedProcess(_ObservedProcess):
    def create_time(self) -> float:
        return 101.0


def test_sampler_rejects_a_reused_pid() -> None:
    sampler = ProcessSampler(
        42,
        expected_create_time=100.25,
        process_factory=_ReusedProcess,
    )

    sample = sampler.sample()

    assert sample.status == "unavailable"
    assert sample.rss_bytes is None
    assert sample.cpu_percent is None
    assert sample.errors == ("pid_reused",)


class _DeniedProcess(_ObservedProcess):
    def memory_info(self) -> _MemoryInfo:
        raise PermissionError("details must not leak")

    def cpu_percent(self, interval: None = None) -> float:
        raise ProcessLookupError("details must not leak")


def test_sampler_degrades_gracefully_when_metrics_are_unavailable() -> None:
    sampler = ProcessSampler(42, process_factory=_DeniedProcess)

    sample = sampler.sample()

    assert sample.status == "unavailable"
    assert sample.rss_bytes is None
    assert sample.cpu_percent is None
    assert sample.errors == (
        "rss_unavailable:PermissionError",
        "cpu_unavailable:ProcessLookupError",
    )


class _MissingProcess:
    def __init__(self, pid: int) -> None:
        raise ProcessLookupError(pid)


def test_sampler_construction_failure_stays_unavailable() -> None:
    sampler = ProcessSampler(42, process_factory=_MissingProcess)

    sample = sampler.sample()

    assert sample.status == "unavailable"
    assert sample.errors == ("identity_unavailable:ProcessLookupError",)
