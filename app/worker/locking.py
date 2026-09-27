import logging

import psycopg

from app.core import locks
from app.core.db import connect_raw

log = logging.getLogger(__name__)


class PgTemplateLocker:
    """Overlap-locks per template op een eigen connectie.

    Een sessie-lock blijft de hele run staan (die beslaat meerdere transacties). Valt de
    connectie weg, bijvoorbeeld bij een crash, dan geeft Postgres de lock vrij.
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

    def reset(self) -> None:
        """Na een fout: connectie weggooien. Daarmee vervallen ook alle locks."""
        if self._conn is not None:
            try:
                self._conn.close()
            except psycopg.Error:
                log.warning("closing lock connection failed", exc_info=True)
        self._conn = None
