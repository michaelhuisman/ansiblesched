"""Uitvoering van één geclaimde run met ansible-runner."""

import logging
import shlex
import shutil
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

import ansible_runner
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import Settings
from app.models import Credential, Inventory, Project, Run, RunStatus, Template
from app.services import queue
from app.services.events import SecretMasker, filter_event
from app.worker.credentials import CredentialError, CredentialRef, CredentialResolver
from app.worker.git import RepoCache

log = logging.getLogger(__name__)

EVENT_BATCH_SIZE = 50
EVENT_FLUSH_INTERVAL_S = 1.0
CANCEL_CHECK_INTERVAL_S = 1.0

# ansible_runner.run(**kwargs) -> Runner; ongetypeerde library.
RunnerFn = Callable[..., Any]

_RUNNER_STATUS = {
    "successful": RunStatus.SUCCESSFUL,
    "failed": RunStatus.FAILED,
    "timeout": RunStatus.TIMEOUT,
    "canceled": RunStatus.CANCELED,
}


class RunSetupError(Exception):
    pass


@dataclass(frozen=True)
class RunSpec:
    run_id: int
    project_id: int
    git_url: str
    branch: str
    playbook_path: str
    inventory_source: str
    inventory_path: str | None
    inventory_content: str | None
    extra_vars: dict[str, Any]  # vrije JSON
    limit: str | None
    tags: str | None
    skip_tags: str | None
    verbosity: int
    timeout_s: int | None
    machine_credential: CredentialRef
    vault_credential: CredentialRef | None


def _ref(cred: Credential) -> CredentialRef:
    return CredentialRef(
        id=cred.id, type=cred.type, openbao_path=cred.openbao_path, openbao_key=cred.openbao_key
    )


def load_spec(session: Session, run_id: int) -> RunSpec:
    with session.begin():
        run = session.get_one(Run, run_id)
        template = session.get_one(Template, run.template_id)
        project = session.get_one(Project, template.project_id)
        inventory = session.get_one(Inventory, template.inventory_id)
        machine = session.get_one(Credential, template.machine_credential_id)
        vault = (
            session.get_one(Credential, template.vault_credential_id)
            if template.vault_credential_id is not None
            else None
        )
        if machine.type != "ssh_key":
            raise RunSetupError(f"machine credential {machine.id} is not of type ssh_key")
        if vault is not None and vault.type != "vault_password":
            raise RunSetupError(f"vault credential {vault.id} is not of type vault_password")
        return RunSpec(
            run_id=run.id,
            project_id=project.id,
            git_url=project.git_url,
            branch=project.branch,
            playbook_path=template.playbook_path,
            inventory_source=inventory.source_type,
            inventory_path=inventory.path,
            inventory_content=inventory.content,
            extra_vars=dict(run.extra_vars),
            limit=run.limit,
            tags=template.tags,
            skip_tags=template.skip_tags,
            verbosity=template.verbosity,
            timeout_s=template.timeout_s,
            machine_credential=_ref(machine),
            vault_credential=_ref(vault) if vault else None,
        )


def _inside(base: Path, relative: str) -> Path:
    """Los een relatief pad op binnen `base`; weiger absolute paden en '..'."""
    rel = PurePosixPath(relative)
    if rel.is_absolute() or ".." in rel.parts:
        raise RunSetupError(f"path must be relative without '..': {relative!r}")
    return base / rel


class EventSink:
    """Filtert events en schrijft ze batchgewijs weg."""

    def __init__(self, sm: sessionmaker[Session], run_id: int, masker: SecretMasker) -> None:
        self._sm = sm
        self._run_id = run_id
        self._masker = masker
        self._buffer: list[dict[str, Any]] = []
        self._last_flush = time.monotonic()
        # ansible-runner leest stats van disk, maar we schrijven events niet weg.
        self.stats: dict[str, Any] | None = None

    def handle(self, raw: Mapping[str, Any]) -> bool:
        if raw.get("event") == "playbook_on_stats":
            self.stats = _normalize_stats(raw.get("event_data"))
        event = filter_event(raw, self._masker)
        if event is not None:
            self._buffer.append(event.as_row(self._run_id))
        if (
            len(self._buffer) >= EVENT_BATCH_SIZE
            or time.monotonic() - self._last_flush >= EVENT_FLUSH_INTERVAL_S
        ):
            try:
                self.flush()
            except Exception:
                # Buffer blijft staan; volgende flush probeert het opnieuw.
                log.warning("event flush failed, will retry", exc_info=True)
        # False: ansible-runner schrijft het (ongefilterde) event niet naar disk.
        return False

    def flush(self) -> None:
        if self._buffer:
            with self._sm() as session:
                queue.add_events(session, self._buffer)
            self._buffer = []
        self._last_flush = time.monotonic()


class CancelCheck:
    def __init__(self, sm: sessionmaker[Session], run_id: int) -> None:
        self._sm = sm
        self._run_id = run_id
        self._last_check = 0.0
        self._canceled = False

    def __call__(self) -> bool:
        if self._canceled:
            return True
        now = time.monotonic()
        if now - self._last_check < CANCEL_CHECK_INTERVAL_S:
            return False
        self._last_check = now
        try:
            with self._sm() as session:
                self._canceled = queue.is_cancel_requested(session, self._run_id)
        except Exception:
            log.warning("cancel check failed", exc_info=True)
        return self._canceled


def _normalize_stats(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, Mapping):
        return None
    mapping = {
        "ok": "ok",
        "changed": "changed",
        "failed": "failures",
        "unreachable": "dark",
        "skipped": "skipped",
        "rescued": "rescued",
        "ignored": "ignored",
    }
    return {ours: dict(raw.get(theirs) or {}) for ours, theirs in mapping.items()}


class Executor:
    def __init__(
        self,
        settings: Settings,
        sm: sessionmaker[Session],
        resolver: CredentialResolver,
        repos: RepoCache,
        runner_fn: RunnerFn = ansible_runner.run,
    ) -> None:
        self._settings = settings
        self._sm = sm
        self._resolver = resolver
        self._repos = repos
        self._runner_fn = runner_fn

    def run_dir(self, run_id: int) -> Path:
        return self._settings.runtime_dir / str(run_id)

    def execute(self, run_id: int) -> RunStatus:
        run_dir = self.run_dir(run_id)
        spec: RunSpec | None = None
        status = RunStatus.ERROR
        rc: int | None = None
        stats: dict[str, Any] | None = None
        reason: str | None = None
        try:
            with self._sm() as session:
                spec = load_spec(session, run_id)
            run_dir.mkdir(mode=0o700, parents=False)
            status, rc, stats = self._run(spec, run_dir)
            if status == RunStatus.CANCELED:
                reason = "canceled by user"
            elif status == RunStatus.TIMEOUT:
                reason = f"exceeded timeout of {spec.timeout_s}s"
        except (RunSetupError, CredentialError) as exc:
            reason = str(exc)
            log.warning("run setup failed", extra={"run_id": run_id, "reason": reason})
        except Exception as exc:
            reason = f"internal error: {type(exc).__name__}"
            log.exception("run failed with exception", extra={"run_id": run_id})
        finally:
            shutil.rmtree(run_dir, ignore_errors=True)
            if spec is not None:
                try:
                    self._repos.prune(spec.project_id)
                except Exception:
                    log.warning("worktree prune failed", exc_info=True)
            with self._sm() as session:
                queue.finish(session, run_id, status=status, rc=rc, stats=stats, reason=reason)
            log.info("run finished", extra={"run_id": run_id, "status": status, "rc": rc})
        return status

    def _run(
        self, spec: RunSpec, run_dir: Path
    ) -> tuple[RunStatus, int | None, dict[str, Any] | None]:
        project_dir = run_dir / "project"
        sha = self._repos.checkout(spec.project_id, spec.git_url, spec.branch, project_dir)
        with self._sm() as session:
            queue.set_commit(session, spec.run_id, sha)

        playbook = _inside(project_dir, spec.playbook_path)
        if spec.inventory_source == "inline":
            inventory = run_dir / "inventory" / "hosts"
            inventory.parent.mkdir(mode=0o700)
            inventory.write_text(spec.inventory_content or "")
        else:
            inventory = _inside(project_dir, spec.inventory_path or "")

        ssh_key = self._resolver.resolve(spec.machine_credential)
        if not ssh_key.endswith("\n"):
            ssh_key += "\n"
        secrets = [ssh_key]

        cmdline: str | None = None
        if spec.vault_credential is not None:
            vault_password = self._resolver.resolve(spec.vault_credential)
            secrets.append(vault_password)
            vault_file = run_dir / "vault_password"
            vault_file.touch(mode=0o600)
            vault_file.write_text(vault_password)
            cmdline = f"--vault-password-file {shlex.quote(str(vault_file))}"

        sink = EventSink(self._sm, spec.run_id, SecretMasker(secrets))
        runner = self._runner_fn(
            private_data_dir=str(run_dir),
            project_dir=str(project_dir),
            playbook=str(playbook.relative_to(project_dir)),
            inventory=str(inventory),
            extravars=spec.extra_vars,
            limit=spec.limit,
            tags=spec.tags,
            skip_tags=spec.skip_tags,
            verbosity=spec.verbosity or None,
            ssh_key=ssh_key,
            cmdline=cmdline,
            envvars={
                "ANSIBLE_HOST_KEY_CHECKING": str(self._settings.ansible_host_key_checking),
                "ANSIBLE_RETRY_FILES_ENABLED": "False",
            },
            timeout=spec.timeout_s,
            settings={"pexpect_timeout": 1},
            event_handler=sink.handle,
            cancel_callback=CancelCheck(self._sm, spec.run_id),
            process_isolation=False,
            suppress_env_files=True,
            quiet=True,
        )
        sink.flush()
        status = _RUNNER_STATUS.get(str(runner.status), RunStatus.ERROR)
        rc = runner.rc if isinstance(runner.rc, int) else None
        return status, rc, sink.stats
