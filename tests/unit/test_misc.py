from pathlib import Path

import pytest
from pydantic import ValidationError

from app.api.schemas import InventoryIn, TemplateIn
from app.core.config import Settings
from app.worker.credentials import (
    CredentialError,
    CredentialRef,
    GitAuth,
    UnconfiguredResolver,
    resolve,
    resolve_git,
)


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LAMPLIGHTER_DATABASE_URL", "postgresql+psycopg://u:p@h/db")
    monkeypatch.setenv("LAMPLIGHTER_POLL_INTERVAL_S", "0.5")
    monkeypatch.setenv("LAMPLIGHTER_WORKER_ID", "w1")
    s = Settings()  # type: ignore[call-arg]
    assert s.database_url == "postgresql+psycopg://u:p@h/db"
    assert s.poll_interval_s == 0.5
    assert s.worker_id == "w1"
    assert s.runtime_dir == Path("/run/lamplighter")
    assert s.openbao_enabled is False
    assert s.webhook_openbao_path is None


def test_settings_require_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LAMPLIGHTER_DATABASE_URL", raising=False)
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


class DictResolver:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def fields(self, ref: CredentialRef) -> dict[str, str]:
        return self.values


def test_resolve_picks_key() -> None:
    ref = CredentialRef(1, "ssh_key", "ssh/a", "id_ed25519")
    assert resolve(DictResolver({"id_ed25519": "KEY"}), ref) == "KEY"
    with pytest.raises(CredentialError, match="id_ed25519"):
        resolve(DictResolver({"other": "x"}), ref)


def test_resolve_git_username_default() -> None:
    ref = CredentialRef(2, "git_token", "git/a", "token")
    assert resolve_git(DictResolver({"token": "t"}), ref) == GitAuth("x-access-token", "t")
    assert resolve_git(DictResolver({"token": "t", "username": "bot"}), ref).username == "bot"
    assert "t'" not in repr(resolve_git(DictResolver({"token": "t"}), ref))


def test_unconfigured_resolver() -> None:
    with pytest.raises(CredentialError, match="no credential backend"):
        UnconfiguredResolver().fields(CredentialRef(3, "ssh_key", "x", "y"))
