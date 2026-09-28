"""Reconcile: the schedules table is the source of truth, APScheduler jobs are derived."""

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from apscheduler.schedulers.base import BaseScheduler

from app.scheduler.jobs import enqueue_schedule, job_id, schedule_id_of
from app.scheduler.trigger import InvalidScheduleError, build_trigger
from app.services.schedules import ScheduleSpec

log = logging.getLogger(__name__)


@dataclass
class SyncResult:
    added: list[int] = field(default_factory=list)
    updated: list[int] = field(default_factory=list)
    removed: list[int] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.removed)


def reconcile(scheduler: BaseScheduler, specs: Sequence[ScheduleSpec]) -> SyncResult:
    result = SyncResult()
    desired = {job_id(s.id): s for s in specs}
    existing = {job.id: job for job in scheduler.get_jobs()}

    for jid, spec in desired.items():
        job = existing.get(jid)
        # The job name contains the fingerprint; unchanged jobs are kept so their
        # next_run_time (and thus misfire detection) is preserved.
        if job is not None and job.name == spec.fingerprint:
            continue
        try:
            trigger = build_trigger(spec.cron, spec.timezone)
        except InvalidScheduleError:
            log.exception("invalid schedule in database", extra={"schedule_id": spec.id})
            continue
        scheduler.add_job(
            enqueue_schedule,
            trigger=trigger,
            args=[spec.id],
            id=jid,
            name=spec.fingerprint,
            replace_existing=True,
            misfire_grace_time=spec.misfire_grace_s,
            coalesce=True,
            max_instances=1,
        )
        (result.updated if job is not None else result.added).append(spec.id)

    for jid in existing.keys() - desired.keys():
        sid = schedule_id_of(jid)
        if sid is None:
            continue  # not ours
        scheduler.remove_job(jid)
        result.removed.append(sid)

    if result.changed:
        log.info(
            "schedules synced",
            extra={"added": result.added, "updated": result.updated, "removed": result.removed},
        )
    return result
