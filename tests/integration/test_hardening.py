"""Phase 5a: retention with metrics archive, reaper, lock loss, proxy, scrape token,
known_hosts and version in the static URLs."""

import subprocess
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import hvac
import pytest
from sqlalchemy import insert, select, text

from app.core import locks
from app.core.db import connect_raw, get_engine, get_sessionmaker
from app.models import AuditEntry, Notification, Run, RunEvent, RunStatsArchive
from app.services import retention
from tests.integration.conftest import API_URL, SECRETS_DIR, Env, post, unique
from tests.integration.test_openbao import OPENBAO, put

METRICS_AUTH = {"Authorization": f"Bearer {(SECRETS_DIR / 'metrics-token').read_text().strip()}"}


def metric(name: str, **labels: str) -> float:
    body = httpx.get(f"{API_URL}/metrics", headers=METRICS_AUTH, timeout=10).text
    for line in body.splitlines():
        if line.startswith(name) and all(f'{k}="{v}"' in line for k, v in labels.items()):
            return float(line.rsplit(" ", 1)[1])
    return 0.0


def wait_for(fn: Any, timeout: float, interval: float = 1.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := fn():
            return result
        time.sleep(interval)
    pytest.fail(f"condition not met within {timeout}s")


# --- retentie ------------------------------------------------------------------------


def _insert_run(template_id: int, status: str, age_days: float, duration_s: float = 5) -> int:
    finished = datetime.now(UTC) - timedelta(days=age_days)
    with get_sessionmaker()() as s, s.begin():
        run_id = s.scalar(
            insert(Run)
            .values(
                template_id=template_id,
                triggered_by="test",
                status=status,
                created_at=finished - timedelta(seconds=duration_s + 1),
                started_at=finished - timedelta(seconds=duration_s),
                finished_at=None if status in ("running", "queued") else finished,
                worker_id="retention-test" if status == "running" else None,
            )
            .returning(Run.id)
        )
        s.execute(
            insert(RunEvent),
            [
                {"run_id": run_id, "seq": i, "event": "verbose", "created_at": finished, "data": {}}
                for i in (1, 2)
            ],
        )
    return int(run_id)  # type: ignore[arg-type]


def _count(model: Any, *where: Any) -> int:
    with get_sessionmaker()() as s:
        return len(s.scalars(select(model.id).where(*where)).all())


def test_retention_keeps_metrics_consistent(env: Env) -> None:
    template = env.template("ping.yml")
    name = template["name"]
    # "Live worker" for the running run, otherwise the reaper sets it to error during the test.
    alive = connect_raw("worker:retention-test")
    recent = _insert_run(template["id"], "successful", age_days=1)
    mid = _insert_run(template["id"], "successful", age_days=60)  # events gone, run stays
    old_ok = _insert_run(template["id"], "successful", age_days=200, duration_s=3)
    old_failed = _insert_run(template["id"], "failed", age_days=200, duration_s=700)
    stuck = _insert_run(template["id"], "running", age_days=300)  # never purge
    try:
        before = (
            metric("lamplighter_runs_total", template=name, status="successful"),
            metric("lamplighter_runs_total", template=name, status="failed"),
            metric("lamplighter_run_duration_seconds_count", template=name),
            metric("lamplighter_run_duration_seconds_bucket", template=name, le="5.0"),
        )
        assert before[0] == 3
        with get_sessionmaker()() as s:
            result = retention.run_all(
                s, events_days=30, runs_days=180, audit_days=0, tokens_days=0
            )
        assert result.runs >= 2
        assert result.events >= 2

        assert _count(Run, Run.id.in_([old_ok, old_failed])) == 0
        assert _count(Run, Run.id.in_([recent, mid, stuck])) == 3
        assert _count(RunEvent, RunEvent.run_id == mid) == 0
        assert _count(RunEvent, RunEvent.run_id == recent) == 2
        assert _count(RunEvent, RunEvent.run_id == stuck) == 2

        after = (
            metric("lamplighter_runs_total", template=name, status="successful"),
            metric("lamplighter_runs_total", template=name, status="failed"),
            metric("lamplighter_run_duration_seconds_count", template=name),
            metric("lamplighter_run_duration_seconds_bucket", template=name, le="5.0"),
        )
        assert after == before, "metrics changed after retention"
        with get_sessionmaker()() as s:
            archived = s.scalar(
                select(RunStatsArchive).where(
                    RunStatsArchive.template == name, RunStatsArchive.status == "failed"
                )
            )
        assert archived is not None
        assert archived.runs == 1
        assert archived.duration_buckets["600"] == 0
        assert archived.duration_buckets["1800"] == 1
    finally:
        with get_engine().begin() as conn:
            conn.execute(text("DELETE FROM runs WHERE id = :id"), {"id": stuck})
        alive.close()


def test_retention_zero_means_never(env: Env) -> None:
    template = env.template("ping.yml")
    old = _insert_run(template["id"], "successful", age_days=5000)
    with get_sessionmaker()() as s:
        result = retention.run_all(s, events_days=0, runs_days=0, audit_days=0, tokens_days=0)
    assert (result.events, result.runs, result.audit, result.tokens) == (0, 0, 0, 0)
    assert _count(Run, Run.id == old) == 1
    assert _count(RunEvent, RunEvent.run_id == old) == 2


def test_audit_retention(env: Env) -> None:
    marker = unique("audit-old")
    with get_sessionmaker()() as s, s.begin():
        s.add(AuditEntry(actor="test", action=marker, at=datetime.now(UTC) - timedelta(days=400)))
    with get_sessionmaker()() as s:
        retention.run_all(s, events_days=0, runs_days=0, audit_days=365, tokens_days=0)
    assert _count(AuditEntry, AuditEntry.action == marker) == 0


# --- reaper ----------------------------------------------------------------------------


def test_reaper_errors_runs_of_lost_workers(env: Env) -> None:
    template = env.template("ping.yml")
    with get_sessionmaker()() as s, s.begin():
        run_id = s.scalar(
            insert(Run)
            .values(
                template_id=template["id"],
                triggered_by="test",
                status="running",
                worker_id=f"ghost-{uuid.uuid4().hex[:8]}",
                started_at=datetime.now(UTC) - timedelta(minutes=5),
            )
            .returning(Run.id)
        )
    run = env.wait(int(run_id), timeout=30)  # type: ignore[arg-type]
    assert run["status"] == "error"
    assert run["status_reason"] == "worker lost"
    # Like other errors: a notification (possibly already expanded and sent).
    assert _count(Notification, Notification.run_id == run_id) >= 1


def test_reaper_leaves_live_workers_alone(env: Env) -> None:
    run_id = env.launch(env.template("sleep.yml", extra_vars={"sleep_s": 60}))["id"]
    env.wait(run_id, {"running"})
    time.sleep(20)  # well over grace (10s) + interval (5s) in dev
    assert env.get_run(run_id)["status"] == "running"
    env.api.post(f"/runs/{run_id}/cancel")
    env.wait(run_id, timeout=30)


# --- lock-verlies -----------------------------------------------------------------------


def test_lost_overlap_lock_aborts_run(env: Env) -> None:
    template = env.template("sleep.yml", extra_vars={"sleep_s": 60})
    run_id = env.launch(template)["id"]
    env.wait(run_id, {"running"})

    def holder_pid() -> int | None:
        with get_engine().connect() as conn:
            return conn.execute(
                text(
                    "SELECT pid FROM pg_locks WHERE locktype = 'advisory' AND classid = :ns"
                    " AND objid = :tid AND objsubid = 2 AND granted"
                ),
                {"ns": locks.NS_TEMPLATE, "tid": template["id"]},
            ).scalar()

    pid = wait_for(holder_pid, timeout=10, interval=0.2)
    thief = connect_raw("it:lock-thief")
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
            conn.commit()
        # The test grabs the lock before the worker can take it back.
        wait_for(
            lambda: locks.try_lock(thief, locks.NS_TEMPLATE, template["id"]),
            timeout=5,
            interval=0.05,
        )
        run = env.wait(run_id, timeout=40)
        assert run["status"] == "error"
        assert run["status_reason"] == "overlap lock lost"
    finally:
        thief.close()


# --- proxy, scrape-token en static-URL's ---------------------------------------------------


def test_forwarded_for_from_untrusted_client_is_ignored(env: Env) -> None:
    name = unique("xff")
    httpx.post(
        f"{API_URL}/ui/login",
        data={"username": name, "password": "wrong-password"},
        headers={"X-Forwarded-For": "203.0.113.66"},
    )
    entries = env.api.get("/audit", params={"actor": f"user:local:{name}"}).json()
    assert entries
    assert entries[0]["ip"] != "203.0.113.66"


def test_metrics_requires_scrape_token() -> None:
    assert httpx.get(f"{API_URL}/metrics").status_code == 401
    wrong = httpx.get(f"{API_URL}/metrics", headers={"Authorization": "Bearer nope"})
    assert wrong.status_code == 401
    ok = httpx.get(f"{API_URL}/metrics", headers=METRICS_AUTH)
    assert ok.status_code == 200
    assert "lamplighter_queue_depth" in ok.text


def test_static_urls_are_versioned() -> None:
    page = httpx.get(f"{API_URL}/ui/login").text
    css = next(p for p in page.split('"') if p.startswith("/ui/static/app.css?v="))
    assert httpx.get(f"{API_URL}{css}").status_code == 200


# --- known_hosts -----------------------------------------------------------------------


@pytest.fixture(scope="module")
def bao() -> hvac.Client:
    token = (SECRETS_DIR / "openbao" / "root-token").read_text().strip()
    return hvac.Client(url=OPENBAO, token=token)


def _known_hosts_template(env: Env, path: str) -> dict[str, Any]:
    cred = post(
        env.api,
        "/credentials",
        {
            "name": unique("kh"),
            "type": "known_hosts",
            "openbao_path": path,
            "openbao_key": "known_hosts",
        },
    )
    return env.template("ping.yml", known_hosts_credential_id=cred["id"])


def test_known_hosts_match_succeeds(env: Env) -> None:
    run = env.wait(env.launch(_known_hosts_template(env, "ssh/ssh-target-known-hosts"))["id"])
    assert run["status"] == "successful", run


def test_known_hosts_mismatch_fails(env: Env, bao: hvac.Client, tmp_path: Path) -> None:
    key = tmp_path / "other"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    other = " ".join(key.with_suffix(".pub").read_text().split()[:2])
    path = f"ssh/{unique('wrong-kh')}"
    put(bao, path, known_hosts=f"ssh-target {other}")
    run = env.wait(env.launch(_known_hosts_template(env, path))["id"])
    assert run["status"] == "failed", run
    assert run["stats"]["unreachable"] == {"ssh-target": 1}


def test_template_rejects_wrong_known_hosts_type(env: Env) -> None:
    template = env.template("ping.yml", known_hosts_credential_id=env.credential["id"])
    run = env.wait(env.launch(template)["id"])
    assert run["status"] == "error"
    assert "not of type known_hosts" in run["status_reason"]
