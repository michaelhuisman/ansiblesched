"""Queue claims: two workers must never get the same run."""

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import Engine, create_engine, insert, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.models import Base, Credential, Inventory, Project, Run, RunStatus, Template
from app.services import queue
from tests.integration.conftest import Env

SCHEMA = "it_claim"


class NoopLocker:
    """These tests are about row locks (SKIP LOCKED), not about the template lock."""

    def try_lock(self, template_id: int) -> bool:
        return True

    def unlock(self, template_id: int) -> None:
        pass


@pytest.fixture
def isolated_engine() -> Iterator[Engine]:
    """A separate schema, so the running workers don't see these runs."""
    url = get_settings().database_url
    admin = create_engine(url)
    with admin.begin() as conn:
        conn.execute(text(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE"))
        conn.execute(text(f"CREATE SCHEMA {SCHEMA}"))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={SCHEMA}"}, pool_size=10)
    Base.metadata.create_all(engine)  # tests only
    with Session(engine) as s, s.begin():
        cred = Credential(name="c", type="ssh_key", openbao_path="p", openbao_key="k")
        proj = Project(name="p", git_url="file:///x", branch="main")
        s.add_all([cred, proj])
        s.flush()
        inv = Inventory(name="i", source_type="inline", content="h")
        s.add(inv)
        s.flush()
        s.add(
            Template(
                name="t",
                project_id=proj.id,
                playbook_path="p.yml",
                inventory_id=inv.id,
                machine_credential_id=cred.id,
                extra_vars={},
                verbosity=0,
            )
        )
    yield engine
    engine.dispose()
    with admin.begin() as conn:
        conn.execute(text(f"DROP SCHEMA {SCHEMA} CASCADE"))
    admin.dispose()


def _queue_runs(engine: Engine, n: int) -> list[int]:
    with Session(engine) as s, s.begin():
        template_id = s.scalar(select(Template.id))
        rows = [{"template_id": template_id, "triggered_by": "test"} for _ in range(n)]
        return list(s.scalars(insert(Run).returning(Run.id), rows))


def test_locked_run_is_skipped(isolated_engine: Engine) -> None:
    first, second = _queue_runs(isolated_engine, 2)
    sm = sessionmaker(isolated_engine)
    with sm() as holder, holder.begin():
        # Hold a lock on the first run, like a worker in the middle of its claim.
        holder.execute(select(Run).where(Run.id == first).with_for_update())
        with sm() as other:
            claimed = queue.claim(other, "w2", NoopLocker())
            assert claimed is not None
            assert claimed.run_id == second
            assert queue.claim(other, "w2", NoopLocker()) is None


def test_concurrent_claims_are_unique(isolated_engine: Engine) -> None:
    run_ids = _queue_runs(isolated_engine, 200)
    sm = sessionmaker(isolated_engine)
    claimed: dict[str, list[int]] = {}
    start = threading.Barrier(8)

    def worker(name: str) -> None:
        mine: list[int] = []
        start.wait()
        with sm() as session:
            while (got := queue.claim(session, name, NoopLocker())) is not None:
                mine.append(got.run_id)
        claimed[name] = mine

    with ThreadPoolExecutor(8) as pool:
        list(pool.map(worker, [f"w{i}" for i in range(8)]))

    all_claimed = [r for ids in claimed.values() for r in ids]
    assert sorted(all_claimed) == sorted(run_ids)  # everything claimed, nothing twice
    assert sum(1 for ids in claimed.values() if ids) > 1  # echt concurrent

    with Session(isolated_engine) as s:
        rows = s.execute(select(Run.id, Run.status, Run.worker_id)).all()
    for run_id, status, worker_id in rows:
        assert status == RunStatus.RUNNING
        assert run_id in claimed[worker_id]


def test_scaled_workers_run_each_run_once(env: Env) -> None:
    """With `--scale worker=2`: 20 runs, each executed exactly once.

    Different templates, otherwise the overlap lock makes the runs run one after another.
    """
    templates = [env.template("ping.yml") for _ in range(20)]
    run_ids = [env.launch(t)["id"] for t in templates]
    runs = [env.wait(run_id, timeout=180) for run_id in run_ids]

    assert all(r["status"] == "successful" for r in runs), [r["status"] for r in runs]
    # Duplicate execution would produce duplicate events (unique run_id+seq) or an error.
    counts = {len(env.events(r["id"])) for r in runs}
    assert len(counts) == 1, counts

    workers = {r["worker_id"] for r in runs}
    if len(workers) < 2:
        pytest.skip("only one worker active; start with --scale worker=2")
