"""Prometheus-metrics, bij elke scrape uit Postgres berekend.

api, scheduler en workers zijn losse processen (en replicas); in-process counters zouden
per proces verschillen. De database is de enige bron van waarheid en altijd actueel.
"""

from collections.abc import Iterator

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

DURATION_BUCKETS = (1, 5, 10, 30, 60, 120, 300, 600, 1800, 3600)

_RUNS_BY_STATUS = text(
    "SELECT t.name AS template, r.status, count(*) AS n"
    " FROM runs r JOIN templates t ON t.id = r.template_id"
    " GROUP BY t.name, r.status"
)

_bucket_cols = ", ".join(f"count(*) FILTER (WHERE d <= {b}) AS le_{b}" for b in DURATION_BUCKETS)
_DURATIONS = text(
    # Alleen constante bucketgrenzen in de f-string, geen invoer.
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

_LAST_SUCCESS = text(
    "SELECT r.schedule_id, t.name AS template, extract(epoch FROM max(r.finished_at)) AS ts"
    " FROM runs r JOIN templates t ON t.id = r.template_id"
    " WHERE r.schedule_id IS NOT NULL AND r.status = 'successful'"
    " GROUP BY r.schedule_id, t.name"
)


class RunMetricsCollector(Collector):
    def __init__(self, sm: sessionmaker[Session]) -> None:
        self._sm = sm

    def collect(self) -> Iterator[Metric]:
        with self._sm() as session:
            by_status = session.execute(_RUNS_BY_STATUS).all()
            durations = session.execute(_DURATIONS).all()
            queue = session.execute(_QUEUE).one()
            last_success = session.execute(_LAST_SUCCESS).all()

        runs = CounterMetricFamily(
            "sched_runs", "Runs per template and status", labels=["template", "status"]
        )
        for row in by_status:
            runs.add_metric([row.template, row.status], row.n)
        yield runs

        hist = HistogramMetricFamily(
            "sched_run_duration_seconds",
            "Duration of finished runs",
            labels=["template"],
        )
        for row in durations:
            buckets = [(str(float(b)), getattr(row, f"le_{b}")) for b in DURATION_BUCKETS]
            buckets.append(("+Inf", row.n))
            hist.add_metric([row.template], buckets, sum_value=float(row.total))
        yield hist

        depth = GaugeMetricFamily("sched_queue_depth", "Runs waiting in the queue")
        depth.add_metric([], queue.queued)
        yield depth

        running = GaugeMetricFamily("sched_runs_running", "Runs currently running")
        running.add_metric([], queue.running)
        yield running

        last = GaugeMetricFamily(
            "sched_schedule_last_success_timestamp_seconds",
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
