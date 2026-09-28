"""Scheduler role with leader election via a Postgres advisory lock.

One connection per replica holds the leader lock, listens on `schedules_changed` and
serves as health check. If that connection drops, APScheduler stops immediately and the
replica returns to follower mode.
"""

import logging
import signal
import threading
from types import FrameType

import psycopg
from apscheduler.events import EVENT_JOB_MISSED, JobExecutionEvent
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore
from apscheduler.schedulers.background import BackgroundScheduler

from app.core import locks
from app.core.config import Settings, get_settings
from app.core.db import connect_raw, get_engine, get_sessionmaker
from app.models import JOBSTORE_TABLE
from app.scheduler.jobs import (
    NOTIFICATIONS_INTERVAL_S,
    NOTIFICATIONS_JOB_ID,
    REAPER_JOB_ID,
    RETENTION_JOB_ID,
    SESSION_PURGE_INTERVAL_S,
    SESSION_PURGE_JOB_ID,
    deliver_notifications,
    purge_sessions,
    reap_lost_runs,
    record_missed,
    run_retention,
    schedule_id_of,
)
from app.scheduler.sync import reconcile
from app.services import schedules

log = logging.getLogger(__name__)


def _on_missed(event: JobExecutionEvent) -> None:
    sid = schedule_id_of(event.job_id)
    if sid is None:
        return
    try:
        record_missed(sid, event.scheduled_run_time)
    except Exception:
        log.exception("could not record missed run", extra={"schedule_id": sid})


def _build_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(
        jobstores={
            "default": SQLAlchemyJobStore(engine=get_engine(), tablename=JOBSTORE_TABLE),
            # Internal jobs of the leader; not persistent, not managed by the reconcile.
            "internal": MemoryJobStore(),
        },
        job_defaults={"coalesce": True, "max_instances": 1},
        timezone="UTC",
    )
    scheduler.add_listener(_on_missed, EVENT_JOB_MISSED)
    scheduler.add_job(
        deliver_notifications,
        "interval",
        seconds=NOTIFICATIONS_INTERVAL_S,
        id=NOTIFICATIONS_JOB_ID,
        jobstore="internal",
        max_instances=1,
        coalesce=True,
    )
    settings = get_settings()
    scheduler.add_job(
        run_retention,
        "cron",
        hour=settings.retention_hour_utc,
        minute=0,
        id=RETENTION_JOB_ID,
        jobstore="internal",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        reap_lost_runs,
        "interval",
        seconds=settings.reaper_interval_s,
        id=REAPER_JOB_ID,
        jobstore="internal",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        purge_sessions,
        "interval",
        seconds=SESSION_PURGE_INTERVAL_S,
        id=SESSION_PURGE_JOB_ID,
        jobstore="internal",
        max_instances=1,
        coalesce=True,
    )
    return scheduler


def _sync(scheduler: BackgroundScheduler) -> None:
    with get_sessionmaker()() as session:
        specs = schedules.enabled_specs(session)
    reconcile(scheduler, specs)


def lead(
    conn: psycopg.Connection[tuple[object, ...]], settings: Settings, stop: threading.Event
) -> None:
    """Run as leader until `stop` is set or the lock connection drops."""
    conn.execute(f"LISTEN {schedules.NOTIFY_CHANNEL}")
    scheduler = _build_scheduler()
    # Start paused: first sync the jobs, only then fire.
    scheduler.start(paused=True)
    try:
        _sync(scheduler)
        scheduler.resume()
        log.info("scheduler running as leader")
        while not stop.is_set():
            # Wait for a NOTIFY or the timeout; a broken connection raises here.
            for _ in conn.notifies(timeout=settings.scheduler_sync_interval_s, stop_after=1):
                pass
            conn.execute("SELECT 1")
            _sync(scheduler)
    finally:
        scheduler.shutdown(wait=False)
        log.info("scheduler stopped")


def run_scheduler(settings: Settings) -> int:
    stop = threading.Event()

    def _on_signal(signum: int, _frame: FrameType | None) -> None:
        log.info("stop requested", extra={"signal": signum})
        stop.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    log.info("scheduler started", extra={"worker_id": settings.worker_id})
    if 0 < settings.retention_runs_days < settings.retention_events_days:
        log.warning(
            "retention_events_days is longer than retention_runs_days; events are removed"
            " together with their run"
        )
    while not stop.is_set():
        try:
            with connect_raw(f"scheduler:{settings.worker_id}") as conn:
                if not locks.try_lock(conn, locks.NS_SCHEDULER, locks.SCHEDULER_LEADER_ID):
                    stop.wait(settings.scheduler_lock_retry_s)
                    continue
                log.info("acquired leader lock")
                lead(conn, settings, stop)
        except Exception:
            # Lock connection is closed (with block), so the lock is free for another replica.
            log.exception("leader loop failed, stepping down")
            stop.wait(settings.scheduler_lock_retry_s)
    return 0
