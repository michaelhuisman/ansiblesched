"""UI sessions. The session id goes to the browser as a cookie; only the hash is stored."""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import AuthSession, User

# don't update last_seen_at on every request.
TOUCH_INTERVAL = timedelta(seconds=60)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass(frozen=True)
class NewSession:
    raw_token: str
    csrf_token: str


def create(session: Session, user_id: int, *, oidc_roles: list[str], max_s: int) -> NewSession:
    raw = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    session.add(
        AuthSession(
            token_hash=hash_token(raw),
            user_id=user_id,
            csrf_token=csrf,
            oidc_roles=sorted(set(oidc_roles)),
            expires_at=datetime.now(UTC) + timedelta(seconds=max_s),
        )
    )
    session.commit()
    return NewSession(raw, csrf)


def resolve(session: Session, raw: str, *, idle_s: int) -> tuple[AuthSession, User] | None:
    now = datetime.now(UTC)
    row = session.execute(
        select(AuthSession, User)
        .join(User, User.id == AuthSession.user_id)
        .where(AuthSession.token_hash == hash_token(raw))
    ).first()
    if row is None:
        return None
    auth_session, user = row.tuple()
    idle_deadline = auth_session.last_seen_at + timedelta(seconds=idle_s)
    if user.disabled or auth_session.expires_at <= now or idle_deadline <= now:
        session.delete(auth_session)
        session.commit()
        return None
    if now - auth_session.last_seen_at > TOUCH_INTERVAL:
        auth_session.last_seen_at = now
        session.commit()
    return auth_session, user


def destroy(session: Session, raw: str) -> AuthSession | None:
    row = session.scalar(select(AuthSession).where(AuthSession.token_hash == hash_token(raw)))
    if row is not None:
        session.delete(row)
        session.commit()
    return row


def destroy_for_user(session: Session, user_id: int) -> None:
    """After disabling or a password reset: invalidate all sessions of the user."""
    session.execute(delete(AuthSession).where(AuthSession.user_id == user_id))
    session.commit()


def purge_expired(session: Session) -> int:
    removed = session.scalars(
        delete(AuthSession)
        .where(AuthSession.expires_at <= datetime.now(UTC))
        .returning(AuthSession.id)
    ).all()
    session.commit()
    return len(removed)
