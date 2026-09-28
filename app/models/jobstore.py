"""Table of APScheduler's SQLAlchemyJobStore.

Defined in our metadata so Alembic manages it. The jobstore only creates it itself if it
does not exist yet (checkfirst), so that is a no-op here. The definition must stay equal
to apscheduler.jobstores.sqlalchemy.
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
