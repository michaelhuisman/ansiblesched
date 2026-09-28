"""Persoonlijke API-tokens voor lokale gebruikers.

De token wordt één keer getoond bij aanmaken; daarna bestaat alleen de sha256.
"""

import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ApiToken, User
from app.services import audit
from app.services.errors import ConflictError, InvalidReferenceError, NotFoundError
from app.services.sessions import hash_token

PREFIX = "lamplighter_"
TOUCH_INTERVAL = timedelta(seconds=60)
MAX_DAYS = 365


@dataclass(frozen=True)
class NewToken:
    token: ApiToken
    raw: str


def create(
    session: Session,
    user: User,
    name: str,
    *,
    expires_days: int | None,
    actor: audit.Actor | None = None,
) -> NewToken:
    if user.source != "local":
        raise ConflictError("API tokens are only for local users; OIDC users use their JWT")
    name = name.strip()
    if not name or len(name) > 100:
        raise InvalidReferenceError("token name: 1-100 characters")
    if expires_days is not None and not 1 <= expires_days <= MAX_DAYS:
        raise InvalidReferenceError(f"expires_days: 1-{MAX_DAYS}")
    raw = PREFIX + secrets.token_urlsafe(32)
    token = ApiToken(
        user_id=user.id,
        name=name,
        token_hash=hash_token(raw),
        prefix=raw[: len(PREFIX) + 6],
        expires_at=(datetime.now(UTC) + timedelta(days=expires_days) if expires_days else None),
    )
    session.add(token)
    session.flush()
    audit.record(session, actor, "token.create", "api_token", token.id, {"name": name})
    session.commit()
    return NewToken(token, raw)


def resolve(session: Session, raw: str) -> tuple[ApiToken, User] | None:
    if not raw.startswith(PREFIX):
        return None
    now = datetime.now(UTC)
    row = session.execute(
        select(ApiToken, User)
        .join(User, User.id == ApiToken.user_id)
        .where(ApiToken.token_hash == hash_token(raw))
    ).first()
    if row is None:
        return None
    token, user = row.tuple()
    if token.revoked_at is not None or user.disabled or user.source != "local":
        return None
    if token.expires_at is not None and token.expires_at <= now:
        return None
    if token.last_used_at is None or now - token.last_used_at > TOUCH_INTERVAL:
        token.last_used_at = now
        session.commit()
    return token, user


def list_for_user(session: Session, user_id: int) -> Sequence[ApiToken]:
    return session.scalars(
        select(ApiToken)
        .where(ApiToken.user_id == user_id, ApiToken.revoked_at.is_(None))
        .order_by(ApiToken.created_at.desc())
    ).all()


def revoke(
    session: Session, token_id: int, *, user_id: int, actor: audit.Actor | None = None
) -> None:
    token = session.get(ApiToken, token_id)
    if token is None or token.user_id != user_id or token.revoked_at is not None:
        raise NotFoundError(f"api_tokens {token_id} not found")
    token.revoked_at = datetime.now(UTC)
    audit.record(session, actor, "token.revoke", "api_token", token.id, {"name": token.name})
    session.commit()
