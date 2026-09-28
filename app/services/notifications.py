"""Webhook-notificaties vanuit de outbox (`notifications`), met retry en backoff."""

import hashlib
import hmac
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import SecretStr
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Notification, Run, Template

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 10
BASE_BACKOFF_S = 5
MAX_BACKOFF_S = 600
BATCH = 20
TIMEOUT_S = 10.0
SIGNATURE_HEADER = "X-Scheduler-Signature"


def fingerprint(url: str) -> str:
    """Stabiele, niet-terug-te-rekenen id van een webhook-URL (voor de outbox)."""
    return hashlib.sha256(url.encode()).hexdigest()[:16]


def targets(urls: Sequence[SecretStr]) -> dict[str, str]:
    return {fingerprint(u.get_secret_value()): u.get_secret_value() for u in urls}


def backoff(attempts: int) -> timedelta:
    """Wachttijd na `attempts` mislukte pogingen: 5s, 10s, 20s, ... max 10 min."""
    seconds = min(BASE_BACKOFF_S * 2 ** max(attempts - 1, 0), MAX_BACKOFF_S)
    return timedelta(seconds=seconds)


def sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def build_payload(run: Run, template: Template, event: str, public_url: str) -> dict[str, Any]:
    return {
        "event": event,
        "run_id": run.id,
        "template": {"id": template.id, "name": template.name},
        "schedule_id": run.schedule_id,
        "status": run.status,
        "rc": run.rc,
        "status_reason": run.status_reason,
        "triggered_by": run.triggered_by,
        "scheduled_for": run.scheduled_for.isoformat() if run.scheduled_for else None,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "url": f"{public_url.rstrip('/')}/ui/runs/{run.id}",
    }


@dataclass(frozen=True)
class DeliveryResult:
    sent: int = 0
    retried: int = 0
    failed: int = 0


def deliver_due(
    session: Session,
    client: httpx.Client,
    *,
    urls: Mapping[str, str],
    public_url: str,
    secret: str | None,
    now: datetime | None = None,
) -> DeliveryResult:
    """Verstuur notificaties die aan de beurt zijn. Rijen worden met SKIP LOCKED geclaimd,
    dus meerdere verzenders zitten elkaar niet in de weg."""
    sent = retried = failed = 0
    with session.begin():
        due = session.scalars(
            select(Notification)
            .where(
                Notification.status == "pending",
                Notification.next_attempt_at <= (now or func.now()),
            )
            .order_by(Notification.next_attempt_at)
            .with_for_update(skip_locked=True)
            .limit(BATCH)
        ).all()
        for note in due:
            run = session.get_one(Run, note.run_id)
            template = session.get_one(Template, run.template_id)
            error = _send(client, urls.get(note.target), note, run, template, public_url, secret)
            note.attempts += 1
            if error is None:
                note.status = "sent"
                note.sent_at = datetime.now(UTC)
                note.last_error = None
                sent += 1
            elif note.attempts >= MAX_ATTEMPTS or urls.get(note.target) is None:
                note.status = "failed"
                note.last_error = error
                failed += 1
            else:
                note.last_error = error
                note.next_attempt_at = datetime.now(UTC) + backoff(note.attempts)
                retried += 1
    if sent or retried or failed:
        log.info("webhooks processed", extra={"sent": sent, "retried": retried, "failed": failed})
    return DeliveryResult(sent, retried, failed)


def _send(
    client: httpx.Client,
    url: str | None,
    note: Notification,
    run: Run,
    template: Template,
    public_url: str,
    secret: str | None,
) -> str | None:
    """Geeft None bij succes, anders een foutmelding zonder URL (die kan een token bevatten)."""
    if url is None:
        return "target no longer configured"
    body = json.dumps(build_payload(run, template, note.event, public_url)).encode()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "ansible-scheduler",
        "X-Scheduler-Event": note.event,
        "X-Scheduler-Delivery": str(note.id),
    }
    if secret:
        headers[SIGNATURE_HEADER] = sign(body, secret)
    try:
        resp = client.post(url, content=body, headers=headers, timeout=TIMEOUT_S)
    except httpx.HTTPError as exc:
        return f"request failed: {type(exc).__name__}"
    if resp.is_success:
        return None
    return f"http {resp.status_code}"
