"""Schedules: pass changes to the scheduler and create runs when they fire."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import exists, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.locks import NS_TEMPLATE
from app.models import Run, RunStatus, Schedule, Template

NOTIFY_CHANNEL = "schedules_changed"


def notify_changed(session: Session) -> None:
    """Signal the leader to reconcile. NOTIFY is transactional: delivered on commit."""
    session.execute(text(f"NOTIFY {NOTIFY_CHANNEL}"))
    session.commit()


@dataclass(frozen=True)
class ScheduleSpec:
    """What the scheduler needs from a schedule to create a job."""

    id: int
    cron: str
    timezone: str
    misfire_grace_s: int

    @property
    def fingerprint(self) -> str:
        # Only fields that affect the trigger; extra_vars etc. are read when firing.
        return f"{self.cron}|{self.timezone}|{self.misfire_grace_s}"


def enabled_specs(session: Session) -> list[ScheduleSpec]:
    with session.begin():
        rows = session.execute(
            select(Schedule.id, Schedule.cron, Schedule.timezone, Schedule.misfire_grace_s).where(
                Schedule.enabled.is_(True)
            )
        ).all()
    return [ScheduleSpec(r.id, r.cron, r.timezone, r.misfire_grace_s) for r in rows]


def load(session: Session, schedule_id: int) -> Schedule | None:
    with session.begin():
        return session.get(Schedule, schedule_id)


def _insert_run(session: Session, values: dict[str, object]) -> int | None:
    stmt = (
        insert(Run)
        .values(**values)
        .on_conflict_do_nothing(
            index_elements=[Run.schedule_id, Run.scheduled_for],
            index_where=Run.schedule_id.is_not(None),
        )
        .returning(Run.id)
    )
    with session.begin():
        return session.scalar(stmt)


def template_busy(session: Session, template_id: int) -> bool:
    """Is a run of this template running, or is one already waiting?

    'Running' comes from pg_locks (the worker's overlap lock), not from runs.status:
    a run left in 'running' by a crashed worker then does not block.
    """
    locked = session.scalar(
        text(
            "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype = 'advisory'"
            " AND classid = :ns AND objid = :tid AND objsubid = 2 AND granted)"
        ).bindparams(ns=NS_TEMPLATE, tid=template_id)
    )
    waiting = session.scalar(
        select(exists().where(Run.template_id == template_id, Run.status == RunStatus.QUEUED))
    )
    return bool(locked or waiting)


def enqueue(session: Session, schedule: Schedule, scheduled_for: datetime) -> int | None:
    """Create the run for one firing. Returns None if it already existed (duplicate leader).

    With policy 'skip' and a busy template the run is recorded as skipped right away;
    the worker checks again when claiming (race between creating and claiming).
    """
    with session.begin():
        template = session.get_one(Template, schedule.template_id)
        extra_vars = {**template.extra_vars, **schedule.extra_vars_override}
        limit = template.limit
        skip = schedule.overlap_policy == "skip" and template_busy(session, template.id)
    if skip:
        return _insert_run(
            session,
            {
                "template_id": schedule.template_id,
                "schedule_id": schedule.id,
                "scheduled_for": scheduled_for,
                "triggered_by": "schedule",
                "status": RunStatus.SKIPPED,
                "status_reason": "previous run still active",
                "overlap_policy": schedule.overlap_policy,
                "extra_vars": extra_vars,
                "limit": limit,
                "finished_at": scheduled_for,
            },
        )
    return _insert_run(
        session,
        {
            "template_id": schedule.template_id,
            "schedule_id": schedule.id,
            "scheduled_for": scheduled_for,
            "triggered_by": "schedule",
            "status": RunStatus.QUEUED,
            "overlap_policy": schedule.overlap_policy,
            "extra_vars": extra_vars,
            "limit": limit,
        },
    )


def record_missed(session: Session, schedule: Schedule, scheduled_for: datetime) -> int | None:
    """A firing that fell outside the misfire grace, visible as a skipped run."""
    return _insert_run(
        session,
        {
            "template_id": schedule.template_id,
            "schedule_id": schedule.id,
            "scheduled_for": scheduled_for,
            "triggered_by": "schedule",
            "status": RunStatus.SKIPPED,
            "status_reason": "missed: scheduler unavailable",
            "overlap_policy": schedule.overlap_policy,
            "finished_at": scheduled_for,
        },
    )
