"""Audit log. `record` adds an entry to the caller's current transaction, so the change
and the audit entry commit or fail together."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditEntry


@dataclass(frozen=True)
class Actor:
    """Who does something: `user:local:<name>` or `user:oidc:<sub>`, plus the IP address."""

    name: str
    ip: str | None = None


def record(
    session: Session,
    actor: Actor | None,
    action: str,
    object_type: str | None = None,
    object_id: object = None,
    details: dict[str, Any] | None = None,  # never secrets
) -> None:
    if actor is None:
        return
    session.add(
        AuditEntry(
            actor=actor.name,
            action=action,
            object_type=object_type,
            object_id=None if object_id is None else str(object_id),
            details=details or {},
            ip=actor.ip,
        )
    )


def list_entries(
    session: Session,
    *,
    actor: str | None = None,
    action: str | None = None,
    since: datetime | None = None,
    limit: int = 200,
    offset: int = 0,
) -> Sequence[AuditEntry]:
    stmt = select(AuditEntry).order_by(AuditEntry.at.desc(), AuditEntry.id.desc())
    if actor:
        stmt = stmt.where(AuditEntry.actor == actor)
    if action:
        stmt = stmt.where(AuditEntry.action.startswith(action))
    if since:
        stmt = stmt.where(AuditEntry.at >= since)
    return session.scalars(stmt.limit(limit).offset(offset)).all()
