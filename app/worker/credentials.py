"""Just-in-time ophalen van secretwaarden op basis van een credential-referentie."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class CredentialError(Exception):
    pass


@dataclass(frozen=True)
class CredentialRef:
    id: int
    type: str
    openbao_path: str
    openbao_key: str


class CredentialResolver(Protocol):
    def resolve(self, ref: CredentialRef) -> str: ...


class DevFileResolver:
    """Fase 1: leest `<base_dir>/<openbao_path>/<openbao_key>`. Wordt in fase 4
    vervangen door een OpenBao-resolver met dezelfde interface."""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir.resolve()

    def resolve(self, ref: CredentialRef) -> str:
        path = (self.base_dir / ref.openbao_path / ref.openbao_key).resolve()
        if not path.is_relative_to(self.base_dir):
            raise CredentialError(f"credential {ref.id}: path outside secrets dir")
        try:
            return path.read_text()
        except OSError as exc:
            # Geen pad in de melding: dat is informatie over de secrets-layout.
            raise CredentialError(f"credential {ref.id}: secret not readable") from exc


class UnconfiguredResolver:
    def resolve(self, ref: CredentialRef) -> str:
        raise CredentialError(f"credential {ref.id}: no credential backend configured")
