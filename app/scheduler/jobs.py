"""Functies die APScheduler aanroept. Worden per referentie gepickled in de jobstore:
module en namen niet verplaatsen."""

import logging
from datetime import UTC, datetime, timedelta

import httpx

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.scheduler.trigger import build_trigger, latest_fire_time
from app.services import notifications, schedules, sessions

log = logging.getLogger(__name__)

JOB_PREFIX = "schedule:"


def job_id(schedule_id: int) -> str:
    return f"{JOB_PREFIX}{schedule_id}"


def schedule_id_of(job_id: str) -> int | None:
    if not job_id.startswith(JOB_PREFIX):
        return None
    try:
        return int(job_id.removeprefix(JOB_PREFIX))
    except ValueError:
        return None


def enqueue_schedule(schedule_id: int) -> None:
    sm = get_sessionmaker()
    with sm() as session:
        schedule = schedules.load(session, schedule_id)
        if schedule is None or not schedule.enabled:
            # Race met de reconcile; die haalt de job straks weg.
            return
        now = datetime.now(UTC)
        trigger = build_trigger(schedule.cron, schedule.timezone)
        lookback = timedelta(seconds=schedule.misfire_grace_s + 120)
        scheduled_for = latest_fire_time(trigger, now, lookback)
        if scheduled_for is None:
            log.warning("no fire time found for schedule", extra={"schedule_id": schedule_id})
            return
        run_id = schedules.enqueue(session, schedule, scheduled_for)
    if run_id is None:
        log.info(
            "run already exists for this fire time",
            extra={"schedule_id": schedule_id, "scheduled_for": scheduled_for.isoformat()},
        )
    else:
        log.info(
            "scheduled run queued",
            extra={
                "schedule_id": schedule_id,
                "run_id": run_id,
                "scheduled_for": scheduled_for.isoformat(),
            },
        )


def record_missed(schedule_id: int, scheduled_for: datetime) -> None:
    sm = get_sessionmaker()
    with sm() as session:
        schedule = schedules.load(session, schedule_id)
        if schedule is None:
            return
        run_id = schedules.record_missed(session, schedule, scheduled_for.astimezone(UTC))
    log.warning(
        "schedule fire time missed",
        extra={
            "schedule_id": schedule_id,
            "run_id": run_id,
            "scheduled_for": scheduled_for.isoformat(),
        },
    )


NOTIFICATIONS_JOB_ID = "internal:notifications"
NOTIFICATIONS_INTERVAL_S = 5


def deliver_notifications() -> None:
    settings = get_settings()
    secret = settings.webhook_secret.get_secret_value() if settings.webhook_secret else None
    with httpx.Client(follow_redirects=False) as client, get_sessionmaker()() as session:
        notifications.deliver_due(
            session,
            client,
            urls=notifications.targets(settings.webhook_urls),
            public_url=settings.public_url,
            secret=secret,
        )


SESSION_PURGE_JOB_ID = "internal:purge-sessions"
SESSION_PURGE_INTERVAL_S = 3600


def purge_sessions() -> None:
    with get_sessionmaker()() as session:
        removed = sessions.purge_expired(session)
    if removed:
        log.info("expired sessions purged", extra={"count": removed})
