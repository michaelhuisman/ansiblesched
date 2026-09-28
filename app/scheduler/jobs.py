"""Functies die APScheduler aanroept. Worden per referentie gepickled in de jobstore:
module en namen niet verplaatsen."""

import logging
import time
from datetime import UTC, datetime, timedelta

import httpx

from app.core import locks
from app.core.config import get_settings
from app.core.db import connect_raw, get_sessionmaker
from app.core.openbao import OpenBaoError, get_openbao
from app.scheduler.trigger import build_trigger, latest_fire_time
from app.services import notifications, reaper, retention, schedules, sessions

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


class _WebhookConfigCache:
    """Webhook-config uit OpenBao, `webhook_cache_s` seconden gecachet. Een rotatie in
    OpenBao is zo zonder herstart actief."""

    def __init__(self) -> None:
        self._value: notifications.WebhookConfig | None = None
        self._loaded_at = 0.0

    def get(self) -> notifications.WebhookConfig | None:
        """None als OpenBao (tijdelijk) niet te lezen is: dan niets versturen of
        uitsplitsen, zodat er geen notificatie verloren gaat."""
        settings = get_settings()
        if not settings.webhook_openbao_path:
            return notifications.WebhookConfig(urls=())
        if (
            self._value is not None
            and time.monotonic() - self._loaded_at < settings.webhook_cache_s
        ):
            return self._value
        client = get_openbao()
        if client is None:
            log.warning("webhook_openbao_path is set but OpenBao is not configured")
            return None
        try:
            self._value = notifications.WebhookConfig.from_secret(
                client.read(settings.webhook_openbao_path)
            )
        except (OpenBaoError, ValueError) as exc:
            log.warning("cannot load webhook config", extra={"reason": str(exc)})
            return None
        self._loaded_at = time.monotonic()
        return self._value


_webhooks = _WebhookConfigCache()


def deliver_notifications() -> None:
    config = _webhooks.get()
    if config is None:
        return
    with httpx.Client(follow_redirects=False) as client, get_sessionmaker()() as session:
        notifications.deliver_due(
            session,
            client,
            urls=config.targets(),
            public_url=get_settings().public_url,
            secret=config.hmac_secret,
        )


SESSION_PURGE_JOB_ID = "internal:purge-sessions"
SESSION_PURGE_INTERVAL_S = 3600


def purge_sessions() -> None:
    with get_sessionmaker()() as session:
        removed = sessions.purge_expired(session)
    if removed:
        log.info("expired sessions purged", extra={"count": removed})


RETENTION_JOB_ID = "internal:retention"
REAPER_JOB_ID = "internal:reaper"


def run_retention() -> None:
    """Dagelijks. Met een eigen advisory lock: nooit twee tegelijk, ook niet als het
    leiderschap tijdens de run wisselt."""
    settings = get_settings()
    with connect_raw("scheduler:retention") as conn:
        if not locks.try_lock(conn, locks.NS_SCHEDULER, locks.MAINTENANCE_ID):
            log.info("retention already running elsewhere")
            return
        with get_sessionmaker()() as session:
            retention.run_all(
                session,
                events_days=settings.retention_events_days,
                runs_days=settings.retention_runs_days,
                audit_days=settings.retention_audit_days,
                tokens_days=settings.retention_tokens_days,
            )


def reap_lost_runs() -> None:
    with get_sessionmaker()() as session:
        reaper.reap(session, get_settings().reaper_grace_s)
