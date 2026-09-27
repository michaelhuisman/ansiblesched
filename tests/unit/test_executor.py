"""Executor-tests zonder database en zonder echte ansible-runner."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import Settings
from app.models import RunStatus
from app.worker import executor as executor_mod
from app.worker.credentials import CredentialRef, DevFileResolver
from app.worker.executor import Executor, RunSpec

SHA = "a" * 40


class FakeRepos:
    def __init__(self) -> None:
        self.pruned: list[int] = []

    def checkout(self, project_id: int, git_url: str, branch: str, dest: Path) -> str:
        dest.mkdir()
        (dest / "ping.yml").write_text("- hosts: all\n")
        return SHA

    def prune(self, project_id: int) -> None:
        self.pruned.append(project_id)


class Recorder:
    def __init__(self) -> None:
        self.finished: dict[str, Any] = {}
        self.commit: str | None = None
        self.events: list[dict[str, Any]] = []


@contextmanager
def _dummy_session() -> Iterator[None]:
    yield None


def _spec(**overrides: Any) -> RunSpec:
    values: dict[str, Any] = {
        "run_id": 5,
        "project_id": 1,
        "git_url": "file:///repo.git",
        "branch": "main",
        "playbook_path": "ping.yml",
        "inventory_source": "inline",
        "inventory_path": None,
        "inventory_content": "host1\n",
        "extra_vars": {"a": 1},
        "limit": None,
        "tags": None,
        "skip_tags": None,
        "verbosity": 0,
        "timeout_s": 30,
        "machine_credential": CredentialRef(1, "ssh_key", "ssh", "key"),
        "vault_credential": None,
    }
    values.update(overrides)
    return RunSpec(**values)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    secrets = tmp_path / "secrets"
    (secrets / "ssh").mkdir(parents=True)
    (secrets / "ssh" / "key").write_text("PRIVATE-KEY-CONTENT-123\n")
    (secrets / "vault").mkdir()
    (secrets / "vault" / "pw").write_text("vault-password-456")

    rec = Recorder()
    spec_holder: dict[str, RunSpec] = {"spec": _spec()}
    monkeypatch.setattr(executor_mod, "load_spec", lambda _s, _id: spec_holder["spec"])
    monkeypatch.setattr(
        executor_mod.queue, "set_commit", lambda _s, _id, sha: setattr(rec, "commit", sha)
    )
    monkeypatch.setattr(executor_mod.queue, "finish", lambda _s, _id, **kw: rec.finished.update(kw))
    monkeypatch.setattr(executor_mod.queue, "add_events", lambda _s, rows: rec.events.extend(rows))

    settings = Settings(database_url="postgresql+psycopg://x/y", runtime_dir=runtime)
    repos = FakeRepos()

    def make(runner_fn: Any) -> Executor:
        return Executor(
            settings,
            _dummy_session,  # type: ignore[arg-type]
            DevFileResolver(secrets),
            repos,  # type: ignore[arg-type]
            runner_fn=runner_fn,
        )

    return SimpleNamespace(runtime=runtime, rec=rec, make=make, spec=spec_holder, repos=repos)


def _runner_result(status: str, rc: int) -> SimpleNamespace:
    return SimpleNamespace(status=status, rc=rc)


def test_successful_run(env: SimpleNamespace) -> None:
    seen: dict[str, Any] = {}

    def fake_runner(**kwargs: Any) -> SimpleNamespace:
        seen.update(kwargs)
        # Tijdens de run bestaat de private data dir.
        assert Path(kwargs["private_data_dir"]).is_dir()
        assert Path(kwargs["inventory"]).read_text() == "host1\n"
        kwargs["event_handler"](
            {
                "counter": 1,
                "event": "playbook_on_stats",
                "stdout": "",
                "event_data": {"ok": {"host1": 1}, "changed": {}, "failures": {}, "dark": {}},
            }
        )
        return _runner_result("successful", 0)

    status = env.make(fake_runner).execute(5)

    assert status == RunStatus.SUCCESSFUL
    assert env.rec.commit == SHA
    assert env.rec.finished["status"] == RunStatus.SUCCESSFUL
    assert env.rec.finished["rc"] == 0
    assert env.rec.finished["stats"]["ok"] == {"host1": 1}
    assert len(env.rec.events) == 1
    assert seen["process_isolation"] is False
    assert seen["suppress_env_files"] is True
    assert seen["ssh_key"] == "PRIVATE-KEY-CONTENT-123\n"
    assert seen["playbook"] == "ping.yml"
    assert seen["extravars"] == {"a": 1}
    assert seen["timeout"] == 30
    assert seen["cmdline"] is None
    assert not (env.runtime / "5").exists()
    assert env.repos.pruned == [1]


@pytest.mark.parametrize(
    ("runner_status", "expected"),
    [
        ("failed", RunStatus.FAILED),
        ("timeout", RunStatus.TIMEOUT),
        ("canceled", RunStatus.CANCELED),
        ("something-odd", RunStatus.ERROR),
    ],
)
def test_status_mapping(env: SimpleNamespace, runner_status: str, expected: RunStatus) -> None:
    status = env.make(lambda **_: _runner_result(runner_status, 2)).execute(5)
    assert status == expected
    assert env.rec.finished["status"] == expected
    assert not (env.runtime / "5").exists()


def test_exception_in_runner_cleans_up(env: SimpleNamespace) -> None:
    def boom(**kwargs: Any) -> None:
        (Path(kwargs["private_data_dir"]) / "artifacts").mkdir()
        raise RuntimeError("runner exploded")

    status = env.make(boom).execute(5)

    assert status == RunStatus.ERROR
    assert env.rec.finished["reason"] == "internal error: RuntimeError"
    assert not (env.runtime / "5").exists()
    assert env.repos.pruned == [1]


def test_missing_credential_is_error_and_cleans_up(env: SimpleNamespace) -> None:
    env.spec["spec"] = _spec(machine_credential=CredentialRef(9, "ssh_key", "nope", "key"))
    status = env.make(lambda **_: pytest.fail("runner must not start")).execute(5)
    assert status == RunStatus.ERROR
    assert "credential 9" in env.rec.finished["reason"]
    assert not (env.runtime / "5").exists()


def test_path_traversal_in_playbook_is_rejected(env: SimpleNamespace) -> None:
    env.spec["spec"] = _spec(playbook_path="../../etc/passwd")
    status = env.make(lambda **_: pytest.fail("runner must not start")).execute(5)
    assert status == RunStatus.ERROR
    assert "relative" in env.rec.finished["reason"]
    assert not (env.runtime / "5").exists()


def test_vault_password_file(env: SimpleNamespace) -> None:
    env.spec["spec"] = _spec(vault_credential=CredentialRef(2, "vault_password", "vault", "pw"))
    seen: dict[str, Any] = {}

    def fake_runner(**kwargs: Any) -> SimpleNamespace:
        seen.update(kwargs)
        vault_file = Path(kwargs["cmdline"].split()[-1])
        assert vault_file.read_text() == "vault-password-456"
        assert vault_file.stat().st_mode & 0o077 == 0
        return _runner_result("successful", 0)

    assert env.make(fake_runner).execute(5) == RunStatus.SUCCESSFUL
    assert seen["cmdline"].startswith("--vault-password-file ")
    assert not (env.runtime / "5").exists()


def test_secrets_are_masked_in_events(env: SimpleNamespace) -> None:
    def fake_runner(**kwargs: Any) -> SimpleNamespace:
        kwargs["event_handler"](
            {"counter": 1, "event": "verbose", "stdout": "leak PRIVATE-KEY-CONTENT-123"}
        )
        return _runner_result("successful", 0)

    env.make(fake_runner).execute(5)
    assert "PRIVATE-KEY-CONTENT-123" not in env.rec.events[0]["stdout"]
