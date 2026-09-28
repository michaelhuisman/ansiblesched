"""UI: beheer van projecten, inventories en credentials.

Credentials zijn alleen referenties naar OpenBao; de UI neemt nooit secretwaarden aan.
"""

import html
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

from app.api.deps import SessionDep
from app.api.schemas import CredentialIn, InventoryIn, ProjectIn
from app.core.auth import Principal
from app.models import Credential, Entity, Inventory, Project
from app.models.config import CREDENTIAL_TYPES
from app.services import crud
from app.services.errors import ServiceError
from app.ui.common import CanConfigure, CanRead, FormData, actor, clean, redirect, render, validate

router = APIRouter(prefix="/ui", include_in_schema=False)

Choices = Callable[[SessionDep], dict[str, Any]]


@dataclass(frozen=True)
class Kind:
    slug: str  # URL-deel en templatenaam: /ui/<slug>, <slug>.html, <slug>_form.html
    model: type[Entity]
    schema: type[BaseModel]
    prepare: Callable[[FormData], FormData]  # formulier -> schema-invoer
    choices: Choices


def _no_choices(_session: SessionDep) -> dict[str, Any]:
    return {}


def _project_choices(session: SessionDep) -> dict[str, Any]:
    creds = crud.list_all(session, Credential)
    return {"git_credentials": [c for c in creds if c.type == "git_token"]}


def _inventory_choices(session: SessionDep) -> dict[str, Any]:
    return {"projects": crud.list_all(session, Project)}


def _prepare_inventory(data: FormData) -> FormData:
    # Het formulier stuurt de velden van beide bronnen mee; alleen die van de gekozen bron
    # tellen.
    if data.get("source_type") == "inline":
        data["path"] = None
    else:
        data["content"] = None
    return data


def _keep(data: FormData) -> FormData:
    return data


KINDS = {
    kind.slug: kind
    for kind in (
        Kind("projects", Project, ProjectIn, _keep, _project_choices),
        Kind("inventories", Inventory, InventoryIn, _prepare_inventory, _inventory_choices),
        Kind("credentials", Credential, CredentialIn, _keep, _no_choices),
    )
}


def _names(session: SessionDep, model: type[Entity]) -> dict[int, str]:
    return {obj.id: str(getattr(obj, "name", obj.id)) for obj in crud.list_all(session, model)}


def _list_context(kind: Kind, session: SessionDep) -> dict[str, Any]:
    ctx: dict[str, Any] = {"items": crud.list_all(session, kind.model)}
    if kind.slug in ("projects", "inventories"):
        ctx["credential_names"] = _names(session, Credential)
        ctx["project_names"] = _names(session, Project)
    if kind.slug == "credentials":
        ctx["credential_types"] = CREDENTIAL_TYPES
    return ctx


def _form(
    request: Request,
    session: SessionDep,
    user: Principal,
    kind: Kind,
    item: Entity | None,
    form: FormData,
    errors: dict[str, str] | None = None,
    code: int = 200,
) -> HTMLResponse:
    return render(
        request,
        f"{kind.slug}_form.html",
        user,
        code=code,
        item=item,
        form=form,
        errors=errors or {},
        credential_types=CREDENTIAL_TYPES,
        **kind.choices(session),
    )


def _save(
    request: Request,
    session: SessionDep,
    user: Principal,
    kind: Kind,
    form: FormData,
    obj_id: int | None,
) -> Response:
    errors: dict[str, str] = {}
    parsed = validate(kind.schema, kind.prepare(clean(form)), errors)
    if parsed is not None and not errors:
        try:
            if obj_id is None:
                crud.create(session, kind.model, parsed.model_dump(), actor=actor(request, user))
            else:
                crud.update(
                    session, kind.model, obj_id, parsed.model_dump(), actor=actor(request, user)
                )
        except ServiceError as exc:
            errors["__all__"] = str(exc)
        else:
            return redirect(f"/ui/{kind.slug}")
    item = crud.get(session, kind.model, obj_id) if obj_id else None
    return _form(request, session, user, kind, item, form, errors, code=422)


def _register(kind: Kind) -> None:
    base = f"/{kind.slug}"

    @router.get(base, response_class=HTMLResponse, name=f"{kind.slug}_list")
    def list_page(request: Request, session: SessionDep, user: CanRead) -> HTMLResponse:
        return render(request, f"{kind.slug}.html", user, **_list_context(kind, session))

    @router.get(f"{base}/new", response_class=HTMLResponse, name=f"{kind.slug}_new")
    def new_page(request: Request, session: SessionDep, user: CanConfigure) -> HTMLResponse:
        defaults: FormData = {"branch": "main", "source_type": "project_file"}
        return _form(request, session, user, kind, None, defaults)

    @router.get(f"{base}/{{obj_id}}/edit", response_class=HTMLResponse, name=f"{kind.slug}_edit")
    def edit_page(
        request: Request, obj_id: int, session: SessionDep, user: CanConfigure
    ) -> HTMLResponse:
        item = crud.get(session, kind.model, obj_id)
        form = kind.schema.model_validate(item, from_attributes=True).model_dump()
        return _form(request, session, user, kind, item, form)

    @router.post(base, response_class=HTMLResponse, name=f"{kind.slug}_create")
    async def create(request: Request, session: SessionDep, user: CanConfigure) -> Response:
        return _save(request, session, user, kind, dict(await request.form()), None)

    @router.post(f"{base}/{{obj_id}}", response_class=HTMLResponse, name=f"{kind.slug}_update")
    async def update(
        request: Request, obj_id: int, session: SessionDep, user: CanConfigure
    ) -> Response:
        return _save(request, session, user, kind, dict(await request.form()), obj_id)

    @router.post(
        f"{base}/{{obj_id}}/delete", response_class=HTMLResponse, name=f"{kind.slug}_delete"
    )
    def delete(request: Request, obj_id: int, session: SessionDep, user: CanConfigure) -> Response:
        try:
            crud.delete(session, kind.model, obj_id, actor=actor(request, user))
        except ServiceError as exc:
            return HTMLResponse(
                f'<span class="error">{html.escape(str(exc))}</span>', status_code=409
            )
        return HTMLResponse("", headers={"HX-Refresh": "true"})


for _kind in KINDS.values():
    _register(_kind)
