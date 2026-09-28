"""retention archive and known_hosts

Revision ID: 16d949879b7e
Revises: 88f7d5d917c4
Create Date: 2026-09-28 18:07:51.153792+00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "16d949879b7e"
down_revision: str | None = "88f7d5d917c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "run_stats_archive",
        sa.Column("template", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("runs", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("duration_count", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("duration_sum", sa.Double(), server_default="0", nullable=False),
        sa.Column(
            "duration_buckets",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_run_stats_archive")),
        sa.UniqueConstraint("template", "status", name="uq_run_stats_archive_template_status"),
    )
    op.add_column(
        "templates", sa.Column("known_hosts_credential_id", sa.BigInteger(), nullable=True)
    )
    op.create_foreign_key(
        op.f("fk_templates_known_hosts_credential_id_credentials"),
        "templates",
        "credentials",
        ["known_hosts_credential_id"],
        ["id"],
    )
    # Manual: autogenerate does not see changed CHECK constraints.
    op.drop_constraint(op.f("ck_credentials_type"), "credentials", type_="check")
    op.create_check_constraint(
        op.f("ck_credentials_type"),
        "credentials",
        "type IN ('ssh_key', 'vault_password', 'git_token', 'known_hosts')",
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_templates_known_hosts_credential_id_credentials"), "templates", type_="foreignkey"
    )
    op.drop_column("templates", "known_hosts_credential_id")
    op.execute("DELETE FROM credentials WHERE type = 'known_hosts'")
    op.drop_constraint(op.f("ck_credentials_type"), "credentials", type_="check")
    op.create_check_constraint(
        op.f("ck_credentials_type"),
        "credentials",
        "type IN ('ssh_key', 'vault_password', 'git_token')",
    )
    op.drop_table("run_stats_archive")
