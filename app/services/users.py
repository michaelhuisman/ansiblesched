"""Gebruikers: lokale accounts (wachtwoord, rollen, lockout) en OIDC-accounts."""

import contextlib
import re
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import ROLES, User
from app.services import audit
from app.services.errors import ConflictError, InvalidReferenceError, NotFoundError

MIN_PASSWORD_LEN = 12
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")

_hasher = PasswordHasher()
# Voor onbekende gebruikersnamen: toch een verify doen, zodat de responstijd niet
# verraadt of een naam bestaat.
_DUMMY_HASH = _hasher.hash("dummy-password-for-timing")


class AuthenticationError(Exception):
    """Bewust generiek: geen onderscheid tussen onbekende naam, fout wachtwoord of lockout."""


def _check_roles(roles: Iterable[str]) -> list[str]:
    roles = sorted(set(roles))
    unknown = set(roles) - set(ROLES)
    if unknown:
        raise InvalidReferenceError(f"unknown roles: {', '.join(sorted(unknown))}")
    return roles


def _check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LEN:
        raise InvalidReferenceError(f"password must be at least {MIN_PASSWORD_LEN} characters")


def get(session: Session, user_id: int) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise NotFoundError(f"users {user_id} not found")
    return user


def get_local(session: Session, username: str) -> User | None:
    return session.scalar(
        select(User).where(User.source == "local", User.username == username.lower())
    )


def list_users(session: Session) -> Sequence[User]:
    return session.scalars(select(User).order_by(User.source, User.username)).all()


def create_local(
    session: Session,
    username: str,
    password: str,
    roles: Iterable[str],
    *,
    display_name: str | None = None,
    actor: audit.Actor | None = None,
) -> User:
    username = username.strip().lower()
    if not USERNAME_RE.fullmatch(username):
        raise InvalidReferenceError("username: 2-64 chars, a-z 0-9 . _ -")
    _check_password(password)
    if get_local(session, username) is not None:
        raise ConflictError(f"user {username!r} already exists")
    user = User(
        source="local",
        username=username,
        display_name=display_name or None,
        password_hash=_hasher.hash(password),
        roles=_check_roles(roles),
    )
    session.add(user)
    session.flush()
    audit.record(
        session, actor, "user.create", "user", user.id, {"username": username, "roles": user.roles}
    )
    session.commit()
    return user


def update_local(
    session: Session,
    user_id: int,
    *,
    roles: Iterable[str] | None = None,
    disabled: bool | None = None,
    display_name: str | None = None,
    actor: audit.Actor | None = None,
) -> User:
    user = get(session, user_id)
    if user.source != "local":
        raise ConflictError("OIDC users are managed in the identity provider")
    changes: dict[str, object] = {}
    if roles is not None:
        user.roles = _check_roles(roles)
        changes["roles"] = user.roles
    if disabled is not None:
        user.disabled = disabled
        changes["disabled"] = disabled
    if display_name is not None:
        user.display_name = display_name or None
        changes["display_name"] = user.display_name
    audit.record(session, actor, "user.update", "user", user.id, changes)
    session.commit()
    return user


def set_password(
    session: Session, user_id: int, password: str, *, actor: audit.Actor | None = None
) -> None:
    user = get(session, user_id)
    if user.source != "local":
        raise ConflictError("OIDC users have no local password")
    _check_password(password)
    user.password_hash = _hasher.hash(password)
    user.failed_logins = 0
    user.locked_until = None
    audit.record(session, actor, "user.password", "user", user.id)
    session.commit()


def verify_password(user: User, password: str) -> bool:
    try:
        return _hasher.verify(user.password_hash or _DUMMY_HASH, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def authenticate(
    session: Session,
    username: str,
    password: str,
    *,
    max_failures: int,
    lockout_s: int,
    ip: str | None = None,
) -> User:
    """Controleer een lokale login. Houdt mislukte pogingen en lockout bij en logt alles."""
    now = datetime.now(UTC)
    name = username.strip().lower()
    user = get_local(session, name)
    attempt = audit.Actor(f"user:local:{name}", ip)
    if user is None:
        _dummy_verify(password)
        audit.record(session, attempt, "login.failed", "user", None, {"reason": "unknown"})
        session.commit()
        raise AuthenticationError
    locked = user.locked_until is not None and user.locked_until > now
    if user.disabled or locked or not verify_password(user, password):
        reason = "disabled" if user.disabled else "locked" if locked else "password"
        if reason == "password":
            user.failed_logins += 1
            if user.failed_logins >= max_failures:
                user.locked_until = now + timedelta(seconds=lockout_s)
                user.failed_logins = 0
                reason = "password, now locked"
        audit.record(session, attempt, "login.failed", "user", user.id, {"reason": reason})
        session.commit()
        raise AuthenticationError
    if user.password_hash and _hasher.check_needs_rehash(user.password_hash):
        user.password_hash = _hasher.hash(password)
    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = now
    audit.record(session, attempt, "login", "user", user.id, {"method": "password"})
    session.commit()
    return user


def _dummy_verify(password: str) -> None:
    with contextlib.suppress(VerificationError):
        _hasher.verify(_DUMMY_HASH, password)


def upsert_oidc(
    session: Session,
    subject: str,
    *,
    username: str | None,
    display_name: str | None,
    email: str | None,
) -> User:
    """Maak of werk een OIDC-gebruiker bij (key: `sub`). Commit niet."""
    stmt = (
        insert(User)
        .values(
            source="oidc",
            username=subject,
            display_name=display_name or username,
            email=email,
            last_login_at=datetime.now(UTC),
        )
        .on_conflict_do_update(
            constraint="uq_users_source_username",
            set_={
                "display_name": display_name or username,
                "email": email,
                "last_login_at": datetime.now(UTC),
            },
        )
        .returning(User.id)
    )
    user_id = session.scalar(stmt)
    session.flush()
    return get(session, int(user_id))  # type: ignore[arg-type]
