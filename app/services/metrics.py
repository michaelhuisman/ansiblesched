"""Prometheus metrics, computed from Postgres on every scrape.

api, scheduler and workers are separate processes (and replicas); in-process counters
would differ per process. The database is the single source of truth and always current.
"""

from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field

from prometheus_client import CollectorRegistry
from prometheus_client.core import (
    CounterMetricFamily,
    GaugeMetricFamily,
    HistogramMetricFamily,
    Metric,
)
from prometheus_client.registry import Collector
from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from app.services.retention import DURATION_BUCKETS

_RUNS_BY_STATUS = text(
    "SELECT t.name AS template, r.status, count(*) AS n"
    " FROM runs r JOIN templates t ON t.id = r.template_id"
    " GROUP BY t.name, r.status"
)

_bucket_cols = ", ".join(f"count(*) FILTER (WHERE d <= {b}) AS le_{b}" for b in DURATION_BUCKETS)
_DURATIONS = text(
    # Only constant bucket bounds in the f-string, no input.
    f"SELECT template, {_bucket_cols}, count(*) AS n, coalesce(sum(d), 0) AS total"  # noqa: S608
    " FROM (SELECT t.name AS template,"
    "  extract(epoch FROM r.finished_at - r.started_at) AS d"
    "  FROM runs r JOIN templates t ON t.id = r.template_id"
    "  WHERE r.started_at IS NOT NULL AND r.finished_at IS NOT NULL) x"
    " GROUP BY template"
)

_QUEUE = text(
    "SELECT count(*) FILTER (WHERE status = 'queued') AS queued,"
    " count(*) FILTER (WHERE status = 'running') AS running FROM runs"
)

# Counts of runs already removed by retention.
_ARCHIVE = text(
    "SELECT template, status, runs, duration_count, duration_sum, duration_buckets"
    " FROM run_stats_archive"
)

_LAST_SUCCESS = text(
    "SELECT r.schedule_id, t.name AS template, extract(epoch FROM max(r.finished_at)) AS ts"
    " FROM runs r JOIN templates t ON t.id = r.template_id"
    " WHERE r.schedule_id IS NOT NULL AND r.status = 'successful'"
    " GROUP BY r.schedule_id, t.name"
)


@dataclass
class _Histogram:
    count: int = 0
    total: float = 0.0
    buckets: dict[int, int] = field(default_factory=lambda: dict.fromkeys(DURATION_BUCKETS, 0))


class RunMetricsCollector(Collector):
    def __init__(self, sm: sessionmaker[Session]) -> None:
        self._sm = sm

    def collect(self) -> Iterator[Metric]:
        with self._sm() as session:
            by_status = session.execute(_RUNS_BY_STATUS).all()
            durations = session.execute(_DURATIONS).all()
            queue = session.execute(_QUEUE).one()
            last_success = session.execute(_LAST_SUCCESS).all()
            archive = session.execute(_ARCHIVE).all()

        # Live runs + archive, so retention does not make the counters drop.
        counts: dict[tuple[str, str], int] = defaultdict(int)
        hists: dict[str, _Histogram] = defaultdict(_Histogram)
        for row in by_status:
            counts[(row.template, row.status)] += row.n
        for row in durations:
            h = hists[row.template]
            h.count += row.n
            h.total += float(row.total)
            for b in DURATION_BUCKETS:
                h.buckets[b] += getattr(row, f"le_{b}")
        for row in archive:
            counts[(row.template, row.status)] += row.runs
            h = hists[row.template]
            h.count += row.duration_count
            h.total += float(row.duration_sum)
            for b in DURATION_BUCKETS:
                h.buckets[b] += int((row.duration_buckets or {}).get(str(b), 0))

        runs = CounterMetricFamily(
            "lamplighter_runs", "Runs per template and status", labels=["template", "status"]
        )
        for (template, status), n in sorted(counts.items()):
            runs.add_metric([template, status], n)
        yield runs

        hist = HistogramMetricFamily(
            "lamplighter_run_duration_seconds",
            "Duration of finished runs",
            labels=["template"],
        )
        for template, h in sorted(hists.items()):
            buckets = [(str(float(b)), h.buckets[b]) for b in DURATION_BUCKETS]
            buckets.append(("+Inf", h.count))
            hist.add_metric([template], buckets, sum_value=h.total)
        yield hist

        depth = GaugeMetricFamily("lamplighter_queue_depth", "Runs waiting in the queue")
        depth.add_metric([], queue.queued)
        yield depth

        running = GaugeMetricFamily("lamplighter_runs_running", "Runs currently running")
        running.add_metric([], queue.running)
        yield running

        last = GaugeMetricFamily(
            "lamplighter_schedule_last_success_timestamp_seconds",
            "Finish time of the last successful run per schedule",
            labels=["schedule_id", "template"],
        )
        for row in last_success:
            last.add_metric([str(row.schedule_id), row.template], float(row.ts))
        yield last


def build_registry(sm: sessionmaker[Session]) -> CollectorRegistry:
    registry = CollectorRegistry(auto_describe=False)
    registry.register(RunMetricsCollector(sm))
    return registry
