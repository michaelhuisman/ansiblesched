"""Just-in-time ophalen van secretwaarden op basis van een credential-referentie."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from app.core.openbao import OpenBaoClient, OpenBaoError

DEFAULT_GIT_USERNAME = "x-access-token"


class CredentialError(Exception):
    pass


@dataclass(frozen=True)
class CredentialRef:
    id: int
    type: str
    openbao_path: str
    openbao_key: str


@dataclass(frozen=True)
class GitAuth:
    username: str
    token: str

    def __repr__(self) -> str:  # nooit de token in logs of tracebacks
        return f"GitAuth(username={self.username!r}, token='***')"


class CredentialResolver(Protocol):
    def fields(self, ref: CredentialRef) -> Mapping[str, str]:
        """Alle velden van het secret waar de credential naar verwijst."""
        ...


def resolve(resolver: CredentialResolver, ref: CredentialRef) -> str:
    """De waarde onder `openbao_key` (SSH-key, vault-wachtwoord)."""
    values = resolver.fields(ref)
    if ref.openbao_key not in values:
        raise CredentialError(f"credential {ref.id}: key {ref.openbao_key!r} not found")
    return values[ref.openbao_key]


def resolve_git(resolver: CredentialResolver, ref: CredentialRef) -> GitAuth:
    """Token onder `openbao_key`, gebruikersnaam uit key `username` (optioneel)."""
    values = resolver.fields(ref)
    if ref.openbao_key not in values:
        raise CredentialError(f"credential {ref.id}: key {ref.openbao_key!r} not found")
    return GitAuth(values.get("username") or DEFAULT_GIT_USERNAME, values[ref.openbao_key])


class OpenBaoResolver:
    def __init__(self, client: OpenBaoClient) -> None:
        self._client = client

    def fields(self, ref: CredentialRef) -> Mapping[str, str]:
        try:
            return self._client.read(ref.openbao_path)
        except OpenBaoError as exc:
            raise CredentialError(f"credential {ref.id}: {exc}") from exc


class UnconfiguredResolver:
    def fields(self, ref: CredentialRef) -> Mapping[str, str]:
        raise CredentialError(f"credential {ref.id}: no credential backend configured")
