from collections.abc import Iterator
from functools import lru_cache

import psycopg
from sqlalchemy import Engine, create_engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

# Visible in pg_stat_activity. The worker sets `worker:<id>` here; the reaper uses that
# to see whether a worker is still alive. Postgres truncates at 63 characters.
_application_name = "lamplighter"


def set_application_name(name: str) -> None:
    """Call before the engine is first used (when a role starts)."""
    global _application_name
    if get_engine.cache_info().currsize:
        raise RuntimeError("set_application_name must be called before get_engine()")
    _application_name = name


@lru_cache
def get_engine() -> Engine:
    return create_engine(
        get_settings().database_url,
        pool_pre_ping=True,
        connect_args={"application_name": _application_name},
    )


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False)


def session_scope() -> Iterator[Session]:
    """FastAPI dependency: one session per request."""
    with get_sessionmaker()() as session:
        yield session


def connect_raw(application_name: str) -> psycopg.Connection[tuple[object, ...]]:
    """Dedicated psycopg connection (autocommit) for session locks and LISTEN.

    Outside the pool: the lifetime of the connection is the lifetime of the lock.
    """
    url = make_url(get_settings().database_url).set(drivername="postgresql")
    return psycopg.connect(
        url.render_as_string(hide_password=False),
        autocommit=True,
        application_name=application_name,
    )
