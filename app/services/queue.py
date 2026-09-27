"""Worker-kant van de queue: claimen, status bijwerken, events opslaan."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import func, insert, or_, select, text, update
from sqlalchemy.orm import Session

from app.core.locks import NS_TEMPLATE
from app.models import Run, RunEvent, RunStatus

CLAIM_BATCH = 20


class TemplateLocker(Protocol):
    """Overlap-lock per template; in de worker een sessie-lock op een eigen connectie."""

    def try_lock(self, template_id: int) -> bool: ...

    def unlock(self, template_id: int) -> None: ...


@dataclass(frozen=True)
class Claimed:
    run_id: int
    template_id: int


# Templates waarvan nu ergens een run loopt. Alleen een hint om te voorkomen dat
# wachtende 'queue'-runs de batch vullen; try_lock blijft de echte beslissing.
_BUSY_TEMPLATES = text(
    "runs.template_id NOT IN (SELECT objid::bigint FROM pg_locks"
    " WHERE locktype = 'advisory' AND classid = :ns AND objsubid = 2 AND granted)"
).bindparams(ns=NS_TEMPLATE)


def claim(session: Session, worker_id: str, locker: TemplateLocker) -> Claimed | None:
    """Claim de oudste run waarvan het template vrij is.

    - Concurrerende workers slaan elkaars gelockte rijen over (SKIP LOCKED).
    - Is het template bezet: 'skip'-runs worden direct `skipped`, 'queue'-runs blijven
      staan en de volgende kandidaat wordt geprobeerd.
    """
    candidates = (
        select(Run.id, Run.template_id, Run.overlap_policy)
        .where(Run.status == RunStatus.QUEUED)
        .where(or_(Run.overlap_policy == "skip", _BUSY_TEMPLATES))
        .order_by(Run.created_at, Run.id)
        .with_for_update(skip_locked=True)
        .limit(CLAIM_BATCH)
    )
    locked: int | None = None
    try:
        with session.begin():
            for run_id, template_id, policy in session.execute(candidates).all():
                if locker.try_lock(template_id):
                    locked = template_id
                    session.execute(
                        update(Run)
                        .where(Run.id == run_id)
                        .values(
                            status=RunStatus.RUNNING, worker_id=worker_id, started_at=func.now()
                        )
                    )
                    return Claimed(run_id, template_id)
                if policy == "skip":
                    session.execute(
                        update(Run)
                        .where(Run.id == run_id)
                        .values(
                            status=RunStatus.SKIPPED,
                            status_reason="previous run still active",
                            finished_at=func.now(),
                        )
                    )
        return None
    except BaseException:
        if locked is not None:
            locker.unlock(locked)
        raise


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
