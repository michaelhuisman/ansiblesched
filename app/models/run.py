from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    Double,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    func,
    text,
)
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
        CheckConstraint("overlap_policy IN ('skip', 'queue')", name="overlap_policy"),
        # Vangrail tegen dubbele runs als twee schedulers kort allebei leider denken te zijn.
        Index(
            "uq_runs_schedule_id_scheduled_for",
            "schedule_id",
            "scheduled_for",
            unique=True,
            postgresql_where=text("schedule_id IS NOT NULL"),
        ),
    )

    template_id: Mapped[int] = mapped_column(ForeignKey("templates.id"))
    schedule_id: Mapped[int | None] = mapped_column(ForeignKey("schedules.id", ondelete="SET NULL"))
    scheduled_for: Mapped[datetime | None]
    # Gekopieerd van de schedule bij aanmaken; handmatige runs gedragen zich als 'queue'.
    overlap_policy: Mapped[str] = mapped_column(Text, server_default="queue")
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


class RunStatsArchive(Entity):
    """Tellingen van runs die door de retentie zijn verwijderd, zodat de metrics
    (counters en het duur-histogram) na het opruimen blijven kloppen."""

    __tablename__ = "run_stats_archive"
    __table_args__ = (
        UniqueConstraint("template", "status", name="uq_run_stats_archive_template_status"),
    )

    template: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    runs: Mapped[int] = mapped_column(server_default="0")
    duration_count: Mapped[int] = mapped_column(server_default="0")
    duration_sum: Mapped[float] = mapped_column(Double, server_default="0")
    # Cumulatieve bucket-tellingen: {"1": n, "5": n, ...} (bovengrens in seconden).
    duration_buckets: Mapped[JsonDict] = mapped_column(server_default="{}")
