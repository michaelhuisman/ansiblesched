from collections.abc import Iterator

import pytest
from apscheduler.schedulers.background import BackgroundScheduler

from app.scheduler.jobs import enqueue_schedule, job_id, schedule_id_of
from app.scheduler.sync import reconcile
from app.services.schedules import ScheduleSpec


@pytest.fixture
def scheduler() -> Iterator[BackgroundScheduler]:
    sched = BackgroundScheduler(timezone="UTC")
    sched.start(paused=True)  # don't let jobs fire
    yield sched
    sched.shutdown(wait=False)


def spec(sid: int, cron: str = "* * * * *", tz: str = "UTC", grace: int = 60) -> ScheduleSpec:
    return ScheduleSpec(id=sid, cron=cron, timezone=tz, misfire_grace_s=grace)


def test_adds_jobs(scheduler: BackgroundScheduler) -> None:
    result = reconcile(scheduler, [spec(1), spec(2, "0 3 * * *", "Europe/Amsterdam", 300)])
    assert sorted(result.added) == [1, 2]
    job = scheduler.get_job(job_id(2))
    assert job.func is enqueue_schedule
    assert job.args == (2,)
    assert job.misfire_grace_time == 300
    assert job.coalesce is True
    assert job.name == "0 3 * * *|Europe/Amsterdam|300"


def test_unchanged_is_noop_and_keeps_next_run(scheduler: BackgroundScheduler) -> None:
    reconcile(scheduler, [spec(1, "0 3 * * *")])
    before = scheduler.get_job(job_id(1)).next_run_time
    result = reconcile(scheduler, [spec(1, "0 3 * * *")])
    assert not result.changed
    assert scheduler.get_job(job_id(1)).next_run_time == before


def test_changed_cron_updates_job(scheduler: BackgroundScheduler) -> None:
    reconcile(scheduler, [spec(1, "0 3 * * *")])
    result = reconcile(scheduler, [spec(1, "0 4 * * *")])
    assert result.updated == [1]
    assert scheduler.get_job(job_id(1)).name.startswith("0 4 * * *")
    assert len(scheduler.get_jobs()) == 1


def test_changed_grace_updates_job(scheduler: BackgroundScheduler) -> None:
    reconcile(scheduler, [spec(1, grace=60)])
    assert reconcile(scheduler, [spec(1, grace=120)]).updated == [1]
    assert scheduler.get_job(job_id(1)).misfire_grace_time == 120


def test_removed_or_disabled_schedule_removes_job(scheduler: BackgroundScheduler) -> None:
    reconcile(scheduler, [spec(1), spec(2)])
    result = reconcile(scheduler, [spec(2)])
    assert result.removed == [1]
    assert scheduler.get_job(job_id(1)) is None


def test_foreign_jobs_are_left_alone(scheduler: BackgroundScheduler) -> None:
    scheduler.add_job(print, "interval", minutes=5, id="housekeeping")
    reconcile(scheduler, [])
    assert scheduler.get_job("housekeeping") is not None


def test_invalid_schedule_is_skipped(scheduler: BackgroundScheduler) -> None:
    result = reconcile(scheduler, [spec(1, "not a cron"), spec(2)])
    assert result.added == [2]


def test_job_id_roundtrip() -> None:
    assert schedule_id_of(job_id(42)) == 42
    assert schedule_id_of("housekeeping") is None
    assert schedule_id_of("schedule:abc") is None
