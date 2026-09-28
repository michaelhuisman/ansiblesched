"""Postgres advisory locks (session level) on a dedicated connection.

Keys use the two-int4 form (namespace, id) so they don't collide with locks of other
applications. In pg_locks: classid = namespace, objid = id, objsubid = 2.
"""

import psycopg

NS_SCHEDULER = 0x5343  # "SC"
NS_TEMPLATE = 0x5450  # "TP"
SCHEDULER_LEADER_ID = 1
MAINTENANCE_ID = 2  # retention: never two at once, not even around a failover

_INT4_MAX = 2**31 - 1


def _key(obj_id: int) -> int:
    if not 0 <= obj_id <= _INT4_MAX:
        raise ValueError(f"advisory lock id out of int4 range: {obj_id}")
    return obj_id


def try_lock(conn: psycopg.Connection[tuple[object, ...]], namespace: int, obj_id: int) -> bool:
    row = conn.execute(
        "SELECT pg_try_advisory_lock(%s::int4, %s::int4)", (namespace, _key(obj_id))
    ).fetchone()
    return bool(row and row[0])


def unlock(conn: psycopg.Connection[tuple[object, ...]], namespace: int, obj_id: int) -> None:
    conn.execute("SELECT pg_advisory_unlock(%s::int4, %s::int4)", (namespace, _key(obj_id)))
