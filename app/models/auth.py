from datetime import datetime

from sqlalchemy import ARRAY, CheckConstraint, ForeignKey, Index, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Entity, JsonDict

ROLES = ("viewer", "operator", "admin")


class User(Entity):
    """Local and OIDC users. OIDC users are created on their first login;
    their roles come from the token and are not stored here."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("source IN ('local', 'oidc')", name="source"),
        CheckConstraint(
            "(source = 'local') OR (password_hash IS NULL AND roles = '{}')", name="oidc_fields"
        ),
        UniqueConstraint("source", "username", name="uq_users_source_username"),
    )

    source: Mapped[str] = mapped_column(Text)
    username: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    password_hash: Mapped[str | None] = mapped_column(Text)
    roles: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    disabled: Mapped[bool] = mapped_column(server_default="false")
    failed_logins: Mapped[int] = mapped_column(server_default="0")
    locked_until: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_login_at: Mapped[datetime | None]


class AuthSession(Entity):
    """UI session. Only the sha256 of the session id is stored here."""

    __tablename__ = "sessions"
    __table_args__ = (Index("ix_sessions_expires_at", "expires_at"),)

    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    csrf_token: Mapped[str] = mapped_column(Text)
    # Roles from the OIDC token at login time; empty for local users
    # (those are read from `users` on every request).
    oidc_roles: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime]


class ApiToken(Entity):
    """Personal API token of a local user. Only the sha256 is stored here."""

    __tablename__ = "api_tokens"

    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(Text)
    token_hash: Mapped[str] = mapped_column(Text, unique=True)
    # First characters of the token, to recognise it in the UI.
    prefix: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    expires_at: Mapped[datetime | None]
    last_used_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]


class AuditEntry(Entity):
    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_at", "at"),
        Index("ix_audit_log_object", "object_type", "object_id"),
    )

    at: Mapped[datetime] = mapped_column(server_default=func.now())
    actor: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    object_type: Mapped[str | None] = mapped_column(Text)
    object_id: Mapped[str | None] = mapped_column(Text)
    details: Mapped[JsonDict] = mapped_column(server_default="{}")
    ip: Mapped[str | None] = mapped_column(Text)
