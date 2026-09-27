"""Worker-kant van de queue: claimen, status bijwerken, events opslaan."""

from collections.abc import Sequence
from typing import Any

from sqlalchemy import func, insert, select, update
from sqlalchemy.orm import Session

from app.models import Run, RunEvent, RunStatus


def claim(session: Session, worker_id: str) -> int | None:
    """Claim de oudste queued run. Concurrerende workers slaan gelockte rijen over."""
    with session.begin():
        run_id = session.scalar(
            select(Run.id)
            .where(Run.status == RunStatus.QUEUED)
            .order_by(Run.created_at, Run.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if run_id is None:
            return None
        session.execute(
            update(Run)
            .where(Run.id == run_id)
            .values(status=RunStatus.RUNNING, worker_id=worker_id, started_at=func.now())
        )
    return run_id


def set_commit(session: Session, run_id: int, commit_sha: str) -> None:
    with session.begin():
        session.execute(update(Run).where(Run.id == run_id).values(commit_sha=commit_sha))


def is_cancel_requested(session: Session, run_id: int) -> bool:
    with session.begin():
        return session.scalar(select(Run.cancel_requested_at).where(Run.id == run_id)) is not None


def finish(
    session: Session,
    run_id: int,
    *,
    status: RunStatus,
    rc: int | None = None,
    stats: dict[str, Any] | None = None,  # ansible-runner stats: JSON
    reason: str | None = None,
) -> None:
    with session.begin():
        session.execute(
            update(Run)
            .where(Run.id == run_id, Run.status == RunStatus.RUNNING)
            .values(
                status=status,
                rc=rc,
                stats=stats,
                status_reason=reason,
                finished_at=func.now(),
            )
        )


# Rijen voor run_events; de sleutels komen overeen met de kolommen.
def add_events(session: Session, rows: Sequence[dict[str, Any]]) -> None:
    if not rows:
        return
    with session.begin():
        session.execute(insert(RunEvent), list(rows))


def fail_orphaned(session: Session, worker_id: str) -> list[int]:
    """Runs die nog op 'running' staan voor deze worker zijn bij een crash blijven hangen."""
    with session.begin():
        result = session.execute(
            update(Run)
            .where(Run.status == RunStatus.RUNNING, Run.worker_id == worker_id)
            .values(
                status=RunStatus.ERROR, status_reason="worker restarted", finished_at=func.now()
            )
            .returning(Run.id)
        )
        return list(result.scalars())


def active_elsewhere(session: Session, run_ids: Sequence[int], worker_id: str) -> set[int]:
    """Welke van deze runs draaien nu bij een andere worker."""
    if not run_ids:
        return set()
    with session.begin():
        return set(
            session.scalars(
                select(Run.id).where(
                    Run.id.in_(run_ids),
                    Run.status == RunStatus.RUNNING,
                    Run.worker_id != worker_id,
                )
            )
        )
