from pathlib import Path

import pytest
from pydantic import ValidationError

from app.api.schemas import InventoryIn, TemplateIn
from app.core.config import Settings
from app.worker.credentials import CredentialError, CredentialRef, DevFileResolver


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCHED_DATABASE_URL", "postgresql+psycopg://u:p@h/db")
    monkeypatch.setenv("SCHED_POLL_INTERVAL_S", "0.5")
    monkeypatch.setenv("SCHED_WORKER_ID", "w1")
    s = Settings()  # type: ignore[call-arg]
    assert s.database_url == "postgresql+psycopg://u:p@h/db"
    assert s.poll_interval_s == 0.5
    assert s.worker_id == "w1"
    assert s.runtime_dir == Path("/run/scheduler")
    assert s.dev_secrets_dir is None


def test_settings_require_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SCHED_DATABASE_URL", raising=False)
    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


@pytest.mark.parametrize("path", ["/etc/hosts", "../x.yml", "a/../../b.yml", ""])
def test_template_rejects_unsafe_playbook_path(path: str) -> None:
    with pytest.raises(ValidationError):
        TemplateIn(
            name="t",
            project_id=1,
            playbook_path=path,
            inventory_id=1,
            machine_credential_id=1,
        )


def test_template_verbosity_bounds() -> None:
    with pytest.raises(ValidationError):
        TemplateIn(
            name="t",
            project_id=1,
            playbook_path="a.yml",
            inventory_id=1,
            machine_credential_id=1,
            verbosity=6,
        )


def test_inventory_source_validation() -> None:
    InventoryIn(name="i", source_type="inline", content="h\n")
    InventoryIn(name="i", source_type="project_file", project_id=1, path="inv/hosts")
    with pytest.raises(ValidationError):
        InventoryIn(name="i", source_type="inline")
    with pytest.raises(ValidationError):
        InventoryIn(name="i", source_type="project_file", path="inv/hosts")
    with pytest.raises(ValidationError):
        InventoryIn(name="i", source_type="inline", content="h", path="x")


def test_dev_resolver_reads_secret(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "k").write_text("value")
    assert DevFileResolver(tmp_path).resolve(CredentialRef(1, "ssh_key", "a", "k")) == "value"


def test_dev_resolver_blocks_escape(tmp_path: Path) -> None:
    base = tmp_path / "secrets"
    base.mkdir()
    (tmp_path / "outside").write_text("nope")
    with pytest.raises(CredentialError, match="outside"):
        DevFileResolver(base).resolve(CredentialRef(1, "ssh_key", "..", "outside"))


def test_dev_resolver_missing_secret_hides_path(tmp_path: Path) -> None:
    with pytest.raises(CredentialError) as exc:
        DevFileResolver(tmp_path).resolve(CredentialRef(3, "ssh_key", "x", "y"))
    assert str(tmp_path) not in str(exc.value)
