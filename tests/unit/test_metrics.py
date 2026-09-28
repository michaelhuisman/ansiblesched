from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

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
    # Drie runs van 3s, 45s en 400s.
    durations = [3, 45, 400]
    buckets = {f"le_{b}": sum(1 for d in durations if d <= b) for b in DURATION_BUCKETS}
    return SimpleNamespace(template="backup", n=3, total=448.0, **buckets)


def _registry_output() -> str:
    results = [
        [
            SimpleNamespace(template="backup", status="successful", n=5),
            SimpleNamespace(template="backup", status="failed", n=2),
        ],
        [_duration_row()],
        [SimpleNamespace(queued=4, running=1)],
        [SimpleNamespace(schedule_id=7, template="backup", ts=1790000000.0)],
    ]

    @contextmanager
    def sm() -> Iterator[FakeSession]:
        yield FakeSession(results)

    return generate_latest(build_registry(sm)).decode()  # type: ignore[arg-type]


def test_metrics_output() -> None:
    out = _registry_output()
    assert 'sched_runs_total{status="successful",template="backup"} 5.0' in out
    assert 'sched_runs_total{status="failed",template="backup"} 2.0' in out
    assert "sched_queue_depth 4.0" in out
    assert "sched_runs_running 1.0" in out
    assert (
        'sched_schedule_last_success_timestamp_seconds{schedule_id="7",template="backup"} 1.79e+09'
        in out
    )


def test_duration_histogram_is_cumulative() -> None:
    out = _registry_output()
    assert 'sched_run_duration_seconds_bucket{le="1.0",template="backup"} 0.0' in out
    assert 'sched_run_duration_seconds_bucket{le="5.0",template="backup"} 1.0' in out
    assert 'sched_run_duration_seconds_bucket{le="60.0",template="backup"} 2.0' in out
    assert 'sched_run_duration_seconds_bucket{le="600.0",template="backup"} 3.0' in out
    assert 'sched_run_duration_seconds_bucket{le="+Inf",template="backup"} 3.0' in out
    assert 'sched_run_duration_seconds_count{template="backup"} 3.0' in out
    assert 'sched_run_duration_seconds_sum{template="backup"} 448.0' in out
