"""Reaper: runs in 'running' whose worker no longer exists.

A worker is alive as long as there is a database connection with
`application_name = worker:<worker_id>` (pool or lock connection). If the worker crashes
or disappears, Postgres closes those connections (TCP keepalives in compose) and the
reaper sets the run to 'error' after `grace_s`.
"""

import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.queue import ALL_TARGETS

log = logging.getLogger(__name__)

REASON = "worker lost"

# Postgres truncates application_name at 63 characters: compare truncated as well.
_REAP = text(
    "UPDATE runs SET status = 'error', status_reason = :reason, finished_at = now()"
    " WHERE status = 'running'"
    "   AND started_at < now() - make_interval(secs => :grace)"
    "   AND NOT EXISTS (SELECT 1 FROM pg_stat_activity a"
    "                   WHERE a.application_name = left('worker:' || runs.worker_id, 63))"
    " RETURNING id"
)

_NOTIFY = text(
    "INSERT INTO notifications (run_id, target, event)"
    " SELECT unnest(CAST(:ids AS bigint[])), :target, 'run.error'"
    " ON CONFLICT DO NOTHING"
)


def reap(session: Session, grace_s: int) -> list[int]:
    """Set orphaned runs to 'error' and (as with other errors) queue a notification."""
    with session.begin():
        ids = list(session.scalars(_REAP, {"reason": REASON, "grace": grace_s}))
        if ids:
            session.execute(_NOTIFY, {"ids": ids, "target": ALL_TARGETS})
    if ids:
        log.warning("reaped runs of lost workers", extra={"run_ids": ids})
    return ids
