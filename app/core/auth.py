"""Auth-naad. Fase 3: iedereen is een anonieme admin (alleen dev).

Fase 4 vervangt `current_user` door OIDC-validatie (Keycloak) en vult de rollen uit
de token. Routes en templates gebruiken alleen `Principal.can`, zodat daar dan niets
hoeft te veranderen.
"""

from dataclasses import dataclass, field
from enum import StrEnum


class Action(StrEnum):
    READ = "read"
    LAUNCH = "launch"
    CANCEL = "cancel"
    CONFIGURE = "configure"


ROLE_ACTIONS: dict[str, frozenset[Action]] = {
    "viewer": frozenset({Action.READ}),
    "operator": frozenset({Action.READ, Action.LAUNCH, Action.CANCEL}),
    "admin": frozenset(Action),
}


@dataclass(frozen=True)
class Principal:
    subject: str
    roles: frozenset[str] = field(default_factory=frozenset)

    def can(self, action: Action) -> bool:
        return any(action in ROLE_ACTIONS.get(role, frozenset()) for role in self.roles)

    @property
    def triggered_by(self) -> str:
        return f"user:{self.subject}"


ANONYMOUS_ADMIN = Principal(subject="anonymous", roles=frozenset({"admin"}))


def current_user() -> Principal:
    return ANONYMOUS_ADMIN
