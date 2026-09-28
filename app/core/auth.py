"""Authentication and authorization.

A request is authenticated via (in this order):
1. `Authorization: Bearer lamplighter_...`: personal API token of a local user;
2. `Authorization: Bearer <jwt>`: access token from the OIDC provider (Keycloak);
3. the UI session cookie. Write requests then require a CSRF token
   (header `X-CSRF-Token` or form field `csrf_token`) and a matching Origin.

Routes and templates only use `Principal.can(action)`.
"""

import hmac
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from functools import lru_cache
from urllib.parse import urlsplit

from fastapi import Request

from app.core.config import get_settings
from app.core.db import get_sessionmaker
from app.core.oidc import OidcClient, OidcError
from app.services import api_tokens, sessions

log = logging.getLogger(__name__)

SESSION_COOKIE = "lamplighter_session"
CSRF_HEADER = "X-CSRF-Token"
CSRF_FIELD = "csrf_token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class Action(StrEnum):
    READ = "read"
    LAUNCH = "launch"
    CANCEL = "cancel"
    CONFIGURE = "configure"
    MANAGE_USERS = "manage_users"


ROLE_ACTIONS: dict[str, frozenset[Action]] = {
    "viewer": frozenset({Action.READ}),
    "operator": frozenset({Action.READ, Action.LAUNCH, Action.CANCEL}),
    "admin": frozenset(Action),
}


@dataclass(frozen=True)
class Principal:
    subject: str  # 'local:<name>' or 'oidc:<sub>'
    roles: frozenset[str] = field(default_factory=frozenset)
    display_name: str | None = None
    user_id: int | None = None
    source: str = "local"
    via: str = "session"  # session | token | bearer
    csrf_token: str | None = None

    def can(self, action: Action) -> bool:
        return any(action in ROLE_ACTIONS.get(role, frozenset()) for role in self.roles)

    @property
    def triggered_by(self) -> str:
        return f"user:{self.subject}"

    @property
    def label(self) -> str:
        return self.display_name or self.subject.split(":", 1)[-1]


class NotAuthenticatedError(Exception):
    pass


class PermissionDeniedError(Exception):
    pass


@lru_cache
def get_oidc_client() -> OidcClient | None:
    return OidcClient.from_settings(get_settings())


def client_ip(request: Request) -> str | None:
    # Behind a reverse proxy (phase 5) this holds X-Forwarded-For with a trusted hop.
    return request.client.host if request.client else None


def _bearer(request: Request) -> str | None:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    return token.strip() if scheme.lower() == "bearer" and token.strip() else None


def _from_api_token(raw: str) -> Principal:
    with get_sessionmaker()() as session:
        found = api_tokens.resolve(session, raw)
    if found is None:
        raise NotAuthenticatedError("invalid or expired API token")
    _, user = found
    return Principal(
        subject=f"local:{user.username}",
        roles=frozenset(user.roles),
        display_name=user.display_name,
        user_id=user.id,
        source="local",
        via="token",
    )


def _from_jwt(raw: str) -> Principal:
    client = get_oidc_client()
    if client is None:
        raise NotAuthenticatedError("bearer JWTs are not accepted (OIDC not configured)")
    try:
        claims = client.validate_access_token(raw)
    except OidcError as exc:
        log.info("rejected bearer token", extra={"reason": str(exc)})
        raise NotAuthenticatedError("invalid token") from exc
    return Principal(
        subject=f"oidc:{claims['sub']}",
        roles=frozenset(client.roles(claims)),
        display_name=claims.get("preferred_username") or claims.get("name"),
        source="oidc",
        via="bearer",
    )


def _from_session(raw: str) -> Principal | None:
    settings = get_settings()
    with get_sessionmaker()() as session:
        found = sessions.resolve(session, raw, idle_s=settings.session_idle_s)
    if found is None:
        return None
    auth_session, user = found
    if user.source == "local":
        subject, roles = f"local:{user.username}", user.roles
    else:
        subject, roles = f"oidc:{user.username}", auth_session.oidc_roles
    return Principal(
        subject=subject,
        roles=frozenset(roles),
        display_name=user.display_name,
        user_id=user.id,
        source=user.source,
        via="session",
        csrf_token=auth_session.csrf_token,
    )


def origin_allowed(request: Request) -> bool:
    origin = request.headers.get("Origin")
    if origin is None:
        return True  # older clients; the CSRF token is the real check
    public = urlsplit(get_settings().public_url)
    got = urlsplit(origin)
    return (got.scheme, got.netloc) == (public.scheme, public.netloc)


async def check_csrf(request: Request, expected: str | None) -> None:
    if not origin_allowed(request):
        raise PermissionDeniedError("cross-origin request refused")
    supplied = request.headers.get(CSRF_HEADER)
    if supplied is None and request.headers.get("content-type", "").startswith(
        ("application/x-www-form-urlencoded", "multipart/form-data")
    ):
        form = await request.form()  # Starlette caches the form for the route
        value = form.get(CSRF_FIELD)
        supplied = value if isinstance(value, str) else None
    if not expected or not supplied or not hmac.compare_digest(supplied, expected):
        raise PermissionDeniedError("missing or invalid CSRF token")


async def authenticate(request: Request) -> Principal | None:
    bearer = _bearer(request)
    if bearer is not None:
        if bearer.startswith(api_tokens.PREFIX):
            return _from_api_token(bearer)
        return _from_jwt(bearer)
    raw = request.cookies.get(SESSION_COOKIE)
    if not raw:
        return None
    principal = _from_session(raw)
    if principal is not None and request.method not in SAFE_METHODS:
        await check_csrf(request, principal.csrf_token)
    return principal


async def current_user(request: Request) -> Principal:
    principal = await authenticate(request)
    if principal is None:
        raise NotAuthenticatedError("authentication required")
    request.state.principal = principal
    return principal
