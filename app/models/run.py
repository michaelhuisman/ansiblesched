from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, ForeignKey, Index, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Entity, JsonDict


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESSFUL = "successful"
    FAILED = "failed"
    ERROR = "error"
    TIMEOUT = "timeout"
    CANCELED = "canceled"
    SKIPPED = "skipped"


FINAL_STATUSES = frozenset(RunStatus) - {RunStatus.QUEUED, RunStatus.RUNNING}


class Run(Entity):
    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in RunStatus) + ")", name="status"
        ),
        Index("ix_runs_status_created_at", "status", "created_at"),
        Index("ix_runs_template_id_created_at", "template_id", "created_at"),
    )

    template_id: Mapped[int] = mapped_column(ForeignKey("templates.id"))
    # schedule_id komt in fase 2 (schedules-tabel bestaat nog niet)
    triggered_by: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=RunStatus.QUEUED.value)
    # Effectieve launch-parameters: template-waarden samengevoegd met overrides.
    extra_vars: Mapped[JsonDict] = mapped_column(server_default="{}")
    limit: Mapped[str | None] = mapped_column(Text)
    commit_sha: Mapped[str | None] = mapped_column(Text)
    worker_id: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    cancel_requested_at: Mapped[datetime | None]
    rc: Mapped[int | None]
    status_reason: Mapped[str | None] = mapped_column(Text)
    stats: Mapped[JsonDict | None]


class RunEvent(Entity):
    __tablename__ = "run_events"
    __table_args__ = (UniqueConstraint("run_id", "seq", name="uq_run_events_run_id_seq"),)

    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    seq: Mapped[int]
    event: Mapped[str] = mapped_column(Text)
    host: Mapped[str | None] = mapped_column(Text)
    task: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime]
    stdout: Mapped[str | None] = mapped_column(Text)
    data: Mapped[JsonDict] = mapped_column(server_default="{}")
