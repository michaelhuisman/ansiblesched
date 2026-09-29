from datetime import datetime

from sqlalchemy import CheckConstraint, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, JsonDict


class MaintenanceStatus(Base):
    """Last outcome per maintenance task (retention, backup), for the metrics.

    The backup writes its row from the backup container with psql; keep the columns
    in sync with `deploy/roles/lamplighter/files/backup-dump.sh`.
    """

    __tablename__ = "maintenance_status"
    __table_args__ = (CheckConstraint("last_status IN ('ok', 'failed')", name="last_status"),)

    task: Mapped[str] = mapped_column(Text, primary_key=True)
    last_attempt_at: Mapped[datetime]
    last_status: Mapped[str] = mapped_column(Text)
    last_success_at: Mapped[datetime | None]
    # Short error message, never secrets.
    last_error: Mapped[str | None] = mapped_column(Text)
    details: Mapped[JsonDict] = mapped_column(server_default="{}")
