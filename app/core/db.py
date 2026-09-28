from collections.abc import Iterator
from functools import lru_cache

import psycopg
from sqlalchemy import Engine, create_engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

# Zichtbaar in pg_stat_activity. De worker zet hier `worker:<id>`; de reaper gebruikt dat
# om te zien of een worker nog leeft. Postgres kapt af op 63 tekens.
_application_name = "lamplighter"


def set_application_name(name: str) -> None:
    """Aanroepen vóór het eerste gebruik van de engine (bij het starten van een rol)."""
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
    """FastAPI-dependency: één sessie per request."""
    with get_sessionmaker()() as session:
        yield session


def connect_raw(application_name: str) -> psycopg.Connection[tuple[object, ...]]:
    """Eigen psycopg-connectie (autocommit) voor sessie-locks en LISTEN.

    Buiten de pool: de levensduur van de connectie is de levensduur van de lock.
    """
    url = make_url(get_settings().database_url).set(drivername="postgresql")
    return psycopg.connect(
        url.render_as_string(hide_password=False),
        autocommit=True,
        application_name=application_name,
    )
