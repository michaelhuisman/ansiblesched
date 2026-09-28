"""Reaper: runs op 'running' waarvan de worker niet meer bestaat.

Een worker is in leven zolang er een databaseverbinding met
`application_name = worker:<worker_id>` is (pool of lock-connectie). Crasht of verdwijnt
de worker, dan sluit Postgres die verbindingen (TCP-keepalives in compose) en zet de
reaper de run na `grace_s` op 'error'.
"""

import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.queue import ALL_TARGETS

log = logging.getLogger(__name__)

REASON = "worker lost"

# application_name wordt door Postgres afgekapt op 63 tekens: vergelijk ook afgekapt.
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
    """Zet weesruns op 'error' en zet (net als bij andere fouten) een notificatie klaar."""
    with session.begin():
        ids = list(session.scalars(_REAP, {"reason": REASON, "grace": grace_s}))
        if ids:
            session.execute(_NOTIFY, {"ids": ids, "target": ALL_TARGETS})
    if ids:
        log.warning("reaped runs of lost workers", extra={"run_ids": ids})
    return ids
