"""schedules and jobstore

Revision ID: 3e0aaccf2ea0
Revises: 8e6c2d089ee8
Create Date: 2026-09-27 19:03:03.895973+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "3e0aaccf2ea0"
down_revision: str | None = "8e6c2d089ee8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "apscheduler_jobs",
        sa.Column("id", sa.Unicode(length=191), nullable=False),
        sa.Column("next_run_time", sa.Float(precision=25), nullable=True),
        sa.Column("job_state", sa.LargeBinary(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_apscheduler_jobs")),
    )
    op.create_index(
        op.f("ix_apscheduler_jobs_next_run_time"),
        "apscheduler_jobs",
        ["next_run_time"],
        unique=False,
    )
    op.create_table(
        "schedules",
        sa.Column("template_id", sa.BigInteger(), nullable=False),
        sa.Column("cron", sa.Text(), nullable=False),
        sa.Column("timezone", sa.Text(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("overlap_policy", sa.Text(), server_default="skip", nullable=False),
        sa.Column("misfire_grace_s", sa.BigInteger(), server_default="60", nullable=False),
        sa.Column(
            "extra_vars_override",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.CheckConstraint(
            "overlap_policy IN ('skip', 'queue')", name=op.f("ck_schedules_overlap_policy")
        ),
        sa.CheckConstraint("misfire_grace_s > 0", name=op.f("ck_schedules_misfire_grace_s")),
        sa.ForeignKeyConstraint(
            ["template_id"], ["templates.id"], name=op.f("fk_schedules_template_id_templates")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_schedules")),
    )
    op.add_column("runs", sa.Column("schedule_id", sa.BigInteger(), nullable=True))
    op.add_column("runs", sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "runs", sa.Column("overlap_policy", sa.Text(), server_default="queue", nullable=False)
    )
    # Manual: autogenerate does not see CHECK constraints on existing tables.
    op.create_check_constraint(
        op.f("ck_runs_overlap_policy"), "runs", "overlap_policy IN ('skip', 'queue')"
    )
    op.create_index(
        "uq_runs_schedule_id_scheduled_for",
        "runs",
        ["schedule_id", "scheduled_for"],
        unique=True,
        postgresql_where=sa.text("schedule_id IS NOT NULL"),
    )
    op.create_foreign_key(
        op.f("fk_runs_schedule_id_schedules"),
        "runs",
        "schedules",
        ["schedule_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(op.f("fk_runs_schedule_id_schedules"), "runs", type_="foreignkey")
    op.drop_index(
        "uq_runs_schedule_id_scheduled_for",
        table_name="runs",
        postgresql_where=sa.text("schedule_id IS NOT NULL"),
    )
    op.drop_constraint(op.f("ck_runs_overlap_policy"), "runs", type_="check")
    op.drop_column("runs", "overlap_policy")
    op.drop_column("runs", "scheduled_for")
    op.drop_column("runs", "schedule_id")
    op.drop_table("schedules")
    op.drop_index(op.f("ix_apscheduler_jobs_next_run_time"), table_name="apscheduler_jobs")
    op.drop_table("apscheduler_jobs")
