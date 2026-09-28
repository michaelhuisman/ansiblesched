"""Scheduling: overlap, sync without restart, leader failover and firing every minute."""

import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any

import psycopg
import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.orm import Session

from app.core import locks
from app.core.db import connect_raw, get_engine, get_sessionmaker
from app.models import Run, Template, apscheduler_jobs
from app.scheduler.jobs import job_id
from app.services import schedules
from tests.integration.conftest import Env, post

LEADER_SQL = text(
    "SELECT a.pid, a.application_name FROM pg_locks l JOIN pg_stat_activity a USING (pid)"
    " WHERE l.locktype = 'advisory' AND l.classid = :ns AND l.objid = :id AND l.granted"
).bindparams(ns=locks.NS_SCHEDULER, id=locks.SCHEDULER_LEADER_ID)


def wait_until[T](fn: Any, timeout: float, interval: float = 0.5) -> T:
    deadline = time.monotonic() + timeout
    while True:
        result = fn()
        if result:
            return result  # type: ignore[no-any-return]
        if time.monotonic() > deadline:
            pytest.fail(f"condition not met within {timeout}s")
        time.sleep(interval)


def leader() -> tuple[int, str] | None:
    with get_engine().connect() as conn:
        row = conn.execute(LEADER_SQL).first()
    return (row.pid, row.application_name) if row else None


def job_next_run(schedule_id: int) -> float | bool | None:
    """next_run_time of the job; False if the job does not exist."""
    with get_engine().connect() as conn:
        row = conn.execute(
            select(apscheduler_jobs.c.next_run_time).where(
                apscheduler_jobs.c.id == job_id(schedule_id)
            )
        ).first()
    return False if row is None else row.next_run_time


def queue_run(template_id: int, policy: str) -> int:
    """Put a run straight into the queue, with the template's extra_vars (like launch)."""
    with Session(get_engine()) as s, s.begin():
        template = s.get_one(Template, template_id)
        return s.scalar(  # type: ignore[no-any-return]
            insert(Run)
            .values(
                template_id=template_id,
                triggered_by="test",
                overlap_policy=policy,
                extra_vars=template.extra_vars,
            )
            .returning(Run.id)
        )


@pytest.fixture
def template_lock(env: Env) -> Iterator[tuple[int, psycopg.Connection[tuple[object, ...]]]]:
    """A sleep template whose overlap lock the test itself holds ('still running')."""
    template = env.template("sleep.yml", extra_vars={"sleep_s": 1})
    conn = connect_raw("it:template-lock")
    assert locks.try_lock(conn, locks.NS_TEMPLATE, template["id"])
    yield template["id"], conn
    conn.close()


# --- overlap -----------------------------------------------------------------


def test_skip_when_template_busy_at_claim(
    env: Env, template_lock: tuple[int, psycopg.Connection[tuple[object, ...]]]
) -> None:
    template_id, _ = template_lock
    run = env.wait(queue_run(template_id, "skip"), timeout=20)
    assert run["status"] == "skipped", run
    assert run["status_reason"] == "previous run still active"


def test_queue_waits_until_template_free(
    env: Env, template_lock: tuple[int, psycopg.Connection[tuple[object, ...]]]
) -> None:
    template_id, conn = template_lock
    run_id = queue_run(template_id, "queue")
    time.sleep(4)  # well over the workers' poll interval
    assert env.get_run(run_id)["status"] == "queued"

    locks.unlock(conn, locks.NS_TEMPLATE, template_id)
    assert env.wait(run_id)["status"] == "successful"


def test_queue_does_not_block_other_templates(
    env: Env, template_lock: tuple[int, psycopg.Connection[tuple[object, ...]]]
) -> None:
    template_id, _ = template_lock
    blocked = [queue_run(template_id, "queue") for _ in range(25)]  # > claim-batch
    other = env.launch(env.template("ping.yml"))
    assert env.wait(other["id"])["status"] == "successful"
    assert all(env.get_run(r)["status"] == "queued" for r in blocked)
    with Session(get_engine()) as s, s.begin():
        s.execute(text("UPDATE runs SET status='canceled' WHERE id = ANY(:ids)"), {"ids": blocked})


def test_manual_runs_queue_behind_each_other(env: Env) -> None:
    template = env.template("sleep.yml", extra_vars={"sleep_s": 3})
    first = env.launch(template)
    second = env.launch(template)
    assert second["overlap_policy"] == "queue"
    a, b = env.wait(first["id"]), env.wait(second["id"])
    assert a["status"] == b["status"] == "successful"
    assert b["started_at"] >= a["finished_at"]


def test_enqueue_skips_when_busy(
    env: Env, template_lock: tuple[int, psycopg.Connection[tuple[object, ...]]]
) -> None:
    template_id, _ = template_lock
    sched = post(
        env.api,
        "/schedules",
        {"template_id": template_id, "cron": "0 0 1 1 *", "enabled": False},
    )
    with get_sessionmaker()() as s:
        schedule = schedules.load(s, sched["id"])
        assert schedule is not None
        when = datetime.now(UTC).replace(microsecond=0)
        run_id = schedules.enqueue(s, schedule, when)
        assert run_id is not None
        # The same firing again (second leader): no second run.
        assert schedules.enqueue(s, schedule, when) is None
    run = env.get_run(run_id)
    assert run["status"] == "skipped"
    assert run["schedule_id"] == sched["id"]


def test_missed_fire_time_is_recorded(env: Env) -> None:
    template = env.template("ping.yml")
    sched = post(
        env.api,
        "/schedules",
        {"template_id": template["id"], "cron": "0 0 1 1 *", "enabled": False},
    )
    when = datetime(2026, 1, 1, tzinfo=UTC)
    with get_sessionmaker()() as s:
        schedule = schedules.load(s, sched["id"])
        assert schedule is not None
        run_id = schedules.record_missed(s, schedule, when)
    run = env.get_run(run_id)  # type: ignore[arg-type]
    assert run["status"] == "skipped"
    assert run["status_reason"].startswith("missed")
    assert datetime.fromisoformat(run["scheduled_for"]) == when


# --- API en sync ---------------------------------------------------------------


def test_schedule_api_validation(env: Env) -> None:
    template = env.template("ping.yml")
    bad = env.api.post("/schedules", json={"template_id": template["id"], "cron": "0 9 1 * 1"})
    assert bad.status_code == 422
    assert "day of week" in bad.text
    unknown = env.api.post("/schedules", json={"template_id": 999999, "cron": "* * * * *"})
    assert unknown.status_code == 422


def test_schedule_changes_apply_without_restart(env: Env) -> None:
    assert leader() is not None, "no scheduler leader running"
    template = env.template("ping.yml")
    sched = post(
        env.api,
        "/schedules",
        {"template_id": template["id"], "cron": "0 0 1 1 *", "timezone": "Europe/Amsterdam"},
    )
    assert sched["next_run_at"] is not None
    first = wait_until(lambda: job_next_run(sched["id"]), timeout=10)

    body = {**sched, "cron": "0 12 * * *"}
    for key in ("id", "created_at", "updated_at", "next_run_at"):
        body.pop(key)
    assert env.api.put(f"/schedules/{sched['id']}", json=body).status_code == 200
    wait_until(lambda: job_next_run(sched["id"]) not in (first, False), timeout=10)

    body["enabled"] = False
    resp = env.api.put(f"/schedules/{sched['id']}", json=body)
    assert resp.json()["next_run_at"] is None
    wait_until(lambda: job_next_run(sched["id"]) is False, timeout=10)

    body["enabled"] = True
    env.api.put(f"/schedules/{sched['id']}", json=body)
    wait_until(lambda: job_next_run(sched["id"]), timeout=10)
    assert env.api.delete(f"/schedules/{sched['id']}").status_code == 204
    wait_until(lambda: job_next_run(sched["id"]) is False, timeout=10)


def test_leader_takes_over_after_connection_loss() -> None:
    old = leader()
    assert old is not None, "no scheduler leader running"
    with get_engine().connect() as conn:
        conn.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": old[0]})
        conn.commit()
    new = wait_until(lambda: (lead := leader()) and lead[0] != old[0] and lead, timeout=30)
    assert new[0] != old[0]


# --- traag: echte afvuringen per minuut --------------------------------------


@pytest.mark.slow
def test_every_minute_exactly_once_and_skip(env: Env) -> None:
    """With --scale scheduler=2: exactly one run every minute. A schedule with a playbook
    that runs longer than a minute gives 'skipped' for the ones in between."""
    ping = post(
        env.api,
        "/schedules",
        {"template_id": env.template("ping.yml")["id"], "cron": "* * * * *"},
    )
    sleeper = post(
        env.api,
        "/schedules",
        {
            "template_id": env.template("sleep.yml", extra_vars={"sleep_s": 75})["id"],
            "cron": "* * * * *",
            "overlap_policy": "skip",
        },
    )
    try:
        # Wait for four firings of the ping schedule (~4 minutes).
        def fired() -> list[dict[str, Any]] | None:
            runs = env.api.get("/runs", params={"limit": 1000}).json()
            mine = [r for r in runs if r["schedule_id"] == ping["id"]]
            return mine if len(mine) >= 4 else None

        ping_runs = wait_until(fired, timeout=300, interval=5)
    finally:
        for sched in (ping, sleeper):
            body = {
                k: v
                for k, v in sched.items()
                if k not in ("id", "created_at", "updated_at", "next_run_at")
            }
            env.api.put(f"/schedules/{sched['id']}", json={**body, "enabled": False})

    times = sorted(datetime.fromisoformat(r["scheduled_for"]) for r in ping_runs)
    assert len(times) == len(set(times)), "duplicate runs for one fire time"
    assert all(b - a == timedelta(minutes=1) for a, b in pairwise(times))
    assert all(t.second == 0 for t in times)

    all_runs = env.api.get("/runs", params={"limit": 1000}).json()
    sleeper_runs = [r for r in all_runs if r["schedule_id"] == sleeper["id"]]
    statuses = [r["status"] for r in sorted(sleeper_runs, key=lambda r: r["scheduled_for"])]
    assert "skipped" in statuses, statuses
    assert statuses.count("running") + statuses.count("successful") >= 1
