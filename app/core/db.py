from collections.abc import Iterator
from functools import lru_cache

import psycopg
from sqlalchemy import Engine, create_engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings


@lru_cache
def get_engine() -> Engine:
    return create_engine(get_settings().database_url, pool_pre_ping=True)


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
