from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Entity


class Notification(Entity):
    """Outbox voor webhooks: één rij per run en per doel.

    `target` is een fingerprint van de webhook-URL, niet de URL zelf: die bevat vaak een
    token en hoort niet in de database.
    """

    __tablename__ = "notifications"
    __table_args__ = (
        CheckConstraint("status IN ('pending', 'sent', 'failed')", name="status"),
        UniqueConstraint("run_id", "target", name="uq_notifications_run_id_target"),
        Index("ix_notifications_status_next_attempt_at", "status", "next_attempt_at"),
    )

    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    target: Mapped[str] = mapped_column(Text)
    event: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default="pending")
    attempts: Mapped[int] = mapped_column(server_default="0")
    next_attempt_at: Mapped[datetime] = mapped_column(server_default=func.now())
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    sent_at: Mapped[datetime | None]
