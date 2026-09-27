"""CRUD-routers voor configuratie-objecten."""

from typing import Annotated

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api import schemas
from app.core.db import session_scope
from app.models import Credential, Entity, Inventory, Project, Template
from app.services import crud

SessionDep = Annotated[Session, Depends(session_scope)]


def crud_router[In: BaseModel, Out: BaseModel](
    prefix: str, model: type[Entity], schema_in: type[In], schema_out: type[Out]
) -> APIRouter:
    router = APIRouter(prefix=f"/{prefix}", tags=[prefix])

    @router.get("", response_model=list[schema_out])  # type: ignore[valid-type]
    def list_items(session: SessionDep) -> list[Out]:
        return [schema_out.model_validate(o) for o in crud.list_all(session, model)]

    @router.post("", response_model=schema_out, status_code=status.HTTP_201_CREATED)
    def create_item(body: schema_in, session: SessionDep) -> Out:  # type: ignore[valid-type]
        obj = crud.create(session, model, body.model_dump())  # type: ignore[attr-defined]
        return schema_out.model_validate(obj)

    @router.get("/{obj_id}", response_model=schema_out)
    def get_item(obj_id: int, session: SessionDep) -> Out:
        return schema_out.model_validate(crud.get(session, model, obj_id))

    @router.put("/{obj_id}", response_model=schema_out)
    def replace_item(obj_id: int, body: schema_in, session: SessionDep) -> Out:  # type: ignore[valid-type]
        obj = crud.update(session, model, obj_id, body.model_dump())  # type: ignore[attr-defined]
        return schema_out.model_validate(obj)

    @router.delete("/{obj_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_item(obj_id: int, session: SessionDep) -> None:
        crud.delete(session, model, obj_id)

    return router


routers = [
    crud_router("projects", Project, schemas.ProjectIn, schemas.ProjectOut),
    crud_router("inventories", Inventory, schemas.InventoryIn, schemas.InventoryOut),
    crud_router("credentials", Credential, schemas.CredentialIn, schemas.CredentialOut),
    crud_router("templates", Template, schemas.TemplateIn, schemas.TemplateOut),
]
