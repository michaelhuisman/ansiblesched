import sys
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from prometheus_client import generate_latest

from app.services.metrics import DURATION_BUCKETS, build_registry


class FakeResult:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self.rows = rows

    def all(self) -> list[SimpleNamespace]:
        return self.rows

    def one(self) -> SimpleNamespace:
        return self.rows[0]


class FakeSession:
    def __init__(self, results: list[list[SimpleNamespace]]) -> None:
        self.results = iter(results)

    def execute(self, _stmt: Any) -> FakeResult:
        return FakeResult(next(self.results))


def _duration_row() -> SimpleNamespace:
    # Three runs of 3s, 45s and 400s.
    durations = [3, 45, 400]
    buckets = {f"le_{b}": sum(1 for d in durations if d <= b) for b in DURATION_BUCKETS}
    return SimpleNamespace(template="backup", n=3, total=448.0, **buckets)


ARCHIVE: list[SimpleNamespace] = []


def _registry_output() -> str:
    results = [
        [
            SimpleNamespace(template="backup", status="successful", n=5),
            SimpleNamespace(template="backup", status="failed", n=2),
        ],
        [_duration_row()],
        [SimpleNamespace(queued=4, running=1)],
        [SimpleNamespace(schedule_id=7, template="backup", ts=1790000000.0)],
        ARCHIVE,
    ]

    @contextmanager
    def sm() -> Iterator[FakeSession]:
        yield FakeSession(results)

    return generate_latest(build_registry(sm)).decode()  # type: ignore[arg-type]


def test_metrics_output() -> None:
    out = _registry_output()
    assert 'lamplighter_runs_total{status="successful",template="backup"} 5.0' in out
    assert 'lamplighter_runs_total{status="failed",template="backup"} 2.0' in out
    assert "lamplighter_queue_depth 4.0" in out
    assert "lamplighter_runs_running 1.0" in out
    assert (
        "lamplighter_schedule_last_success_timestamp_seconds"
        '{schedule_id="7",template="backup"} 1.79e+09' in out
    )


def test_duration_histogram_is_cumulative() -> None:
    out = _registry_output()
    assert 'lamplighter_run_duration_seconds_bucket{le="1.0",template="backup"} 0.0' in out
    assert 'lamplighter_run_duration_seconds_bucket{le="5.0",template="backup"} 1.0' in out
    assert 'lamplighter_run_duration_seconds_bucket{le="60.0",template="backup"} 2.0' in out
    assert 'lamplighter_run_duration_seconds_bucket{le="600.0",template="backup"} 3.0' in out
    assert 'lamplighter_run_duration_seconds_bucket{le="+Inf",template="backup"} 3.0' in out
    assert 'lamplighter_run_duration_seconds_count{template="backup"} 3.0' in out
    assert 'lamplighter_run_duration_seconds_sum{template="backup"} 448.0' in out


def test_archive_is_added_to_counters_and_histogram(monkeypatch: pytest.MonkeyPatch) -> None:
    archived = SimpleNamespace(
        template="backup",
        status="successful",
        runs=10,
        duration_count=10,
        duration_sum=100.0,
        duration_buckets={
            "1": 0,
            "5": 2,
            "10": 5,
            "30": 10,
            "60": 10,
            "120": 10,
            "300": 10,
            "600": 10,
            "1800": 10,
            "3600": 10,
        },
    )
    monkeypatch.setattr(sys.modules[__name__], "ARCHIVE", [archived])
    out = _registry_output()
    assert 'lamplighter_runs_total{status="successful",template="backup"} 15.0' in out
    assert 'lamplighter_runs_total{status="failed",template="backup"} 2.0' in out
    assert 'lamplighter_run_duration_seconds_count{template="backup"} 13.0' in out
    assert 'lamplighter_run_duration_seconds_bucket{le="5.0",template="backup"} 3.0' in out
    assert 'lamplighter_run_duration_seconds_sum{template="backup"} 548.0' in out


def test_archive_only_template_still_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    gone = SimpleNamespace(
        template="deleted-template",
        status="failed",
        runs=3,
        duration_count=0,
        duration_sum=0.0,
        duration_buckets={},
    )
    monkeypatch.setattr(sys.modules[__name__], "ARCHIVE", [gone])
    out = _registry_output()
    assert 'lamplighter_runs_total{status="failed",template="deleted-template"} 3.0' in out
