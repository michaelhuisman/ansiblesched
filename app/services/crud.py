"""Generieke CRUD voor de configuratie-objecten (projects, inventories, ...)."""

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from psycopg import errors as pg_errors
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Entity
from app.services.errors import ConflictError, InvalidReferenceError, NotFoundError


@contextmanager
def _translate_integrity_errors(session: Session) -> Iterator[None]:
    try:
        yield
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        orig = exc.orig
        if isinstance(orig, pg_errors.UniqueViolation):
            raise ConflictError("an object with this name already exists") from exc
        if isinstance(orig, pg_errors.ForeignKeyViolation):
            # Bij insert/update: verwijzing bestaat niet. Bij delete: object is in gebruik.
            if "is still referenced" in str(orig):
                raise ConflictError("object is still in use") from exc
            raise InvalidReferenceError("referenced object does not exist") from exc
        if isinstance(orig, pg_errors.CheckViolation):
            raise InvalidReferenceError(
                f"constraint violated: {orig.diag.constraint_name}"
            ) from exc
        raise


def list_all[M: Entity](session: Session, model: type[M]) -> Sequence[M]:
    return session.scalars(select(model).order_by(model.id)).all()


def get[M: Entity](session: Session, model: type[M], obj_id: int) -> M:
    obj = session.get(model, obj_id)
    if obj is None:
        raise NotFoundError(f"{model.__tablename__} {obj_id} not found")
    return obj


# Waarden komen uit gevalideerde pydantic-schema's (model_dump), vandaar Any.
def create[M: Entity](session: Session, model: type[M], values: Mapping[str, Any]) -> M:
    obj = model(**values)
    with _translate_integrity_errors(session):
        session.add(obj)
        session.flush()
    session.refresh(obj)
    return obj


def update[M: Entity](
    session: Session, model: type[M], obj_id: int, values: Mapping[str, Any]
) -> M:
    obj = get(session, model, obj_id)
    with _translate_integrity_errors(session):
        for key, value in values.items():
            setattr(obj, key, value)
        session.flush()
    session.refresh(obj)
    return obj


def delete[M: Entity](session: Session, model: type[M], obj_id: int) -> None:
    obj = get(session, model, obj_id)
    with _translate_integrity_errors(session):
        session.delete(obj)
        session.flush()
