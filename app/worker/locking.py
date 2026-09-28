import logging

import psycopg

from app.core import locks
from app.core.db import connect_raw

log = logging.getLogger(__name__)


class PgTemplateLocker:
    """Overlap locks per template on a dedicated connection.

    A session lock is held for the whole run (which spans several transactions). If the
    connection drops, for example in a crash, Postgres releases the lock.
    """

    def __init__(self, worker_id: str) -> None:
        self._application_name = f"worker:{worker_id}"
        self._conn: psycopg.Connection[tuple[object, ...]] | None = None

    def _connection(self) -> psycopg.Connection[tuple[object, ...]]:
        if self._conn is None or self._conn.closed:
            self._conn = connect_raw(self._application_name)
        return self._conn

    def try_lock(self, template_id: int) -> bool:
        return locks.try_lock(self._connection(), locks.NS_TEMPLATE, template_id)

    def unlock(self, template_id: int) -> None:
        locks.unlock(self._connection(), locks.NS_TEMPLATE, template_id)

    def ensure(self, template_id: int) -> bool:
        """Is the template lock still ours? On a broken connection: reconnect and take the
        lock again. False if another run has it by now."""
        try:
            self._connection().execute("SELECT 1")
            return True
        except psycopg.Error:
            log.warning("lock connection lost, re-acquiring template lock")
            self.reset()
        try:
            return self.try_lock(template_id)
        except psycopg.Error:
            # Database unreachable: then nobody else can claim either. Let the run continue.
            log.warning("cannot re-acquire template lock, database unavailable")
            return True

    def reset(self) -> None:
        """After an error: discard the connection. That also releases all locks."""
        if self._conn is not None:
            try:
                self._conn.close()
            except psycopg.Error:
                log.warning("closing lock connection failed", exc_info=True)
        self._conn = None
