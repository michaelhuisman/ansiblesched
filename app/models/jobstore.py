"""Tabel van APScheduler's SQLAlchemyJobStore.

Gedefinieerd in onze metadata zodat Alembic hem beheert. De jobstore maakt hem zelf
alleen aan als hij nog niet bestaat (checkfirst), dus dit is dan een no-op. De definitie
moet gelijk blijven aan apscheduler.jobstores.sqlalchemy.
"""

from sqlalchemy import Column, Float, LargeBinary, Table, Unicode

from app.models.base import Base

JOBSTORE_TABLE = "apscheduler_jobs"

apscheduler_jobs = Table(
    JOBSTORE_TABLE,
    Base.metadata,
    Column("id", Unicode(191), primary_key=True),
    Column("next_run_time", Float(25), index=True),
    Column("job_state", LargeBinary, nullable=False),
)
