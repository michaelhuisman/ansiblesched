"""Outcome of maintenance tasks (retention, backup) in `maintenance_status`."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import MaintenanceStatus

MAX_ERROR = 500


def record(
    session: Session,
    task: str,
    *,
    ok: bool,
    error: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Upsert the outcome of one attempt and commit. A failure keeps `last_success_at`."""
    now = datetime.now(UTC)
    values = {
        "task": task,
        "last_attempt_at": now,
        "last_status": "ok" if ok else "failed",
        "last_success_at": now if ok else None,
        "last_error": None if ok else (error or "unknown error")[:MAX_ERROR],
        "details": details or {},
    }
    stmt = insert(MaintenanceStatus).values(values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[MaintenanceStatus.task],
        set_={
            "last_attempt_at": stmt.excluded.last_attempt_at,
            "last_status": stmt.excluded.last_status,
            "last_success_at": func.coalesce(
                stmt.excluded.last_success_at, MaintenanceStatus.last_success_at
            ),
            "last_error": stmt.excluded.last_error,
            "details": stmt.excluded.details,
        },
    )
    session.execute(stmt)
    session.commit()
