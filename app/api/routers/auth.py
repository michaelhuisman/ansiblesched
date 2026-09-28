from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from app.api.deps import ActorDep, SessionDep, UserDep, require
from app.api.schemas import (
    AuditOut,
    MeOut,
    PasswordIn,
    TokenCreate,
    TokenCreated,
    TokenOut,
    UserCreate,
    UserOut,
    UserUpdate,
)
from app.core.auth import Action
from app.services import api_tokens, audit, sessions, users
from app.services.errors import ConflictError

router = APIRouter(tags=["auth"])
ADMIN = [Depends(require(Action.MANAGE_USERS))]


@router.get("/me", response_model=MeOut)
def me(user: UserDep) -> MeOut:
    return MeOut(
        subject=user.subject,
        display_name=user.display_name,
        source=user.source,
        roles=sorted(user.roles),
        via=user.via,
    )


# --- eigen API-tokens (alleen lokale gebruikers) ---------------------------------


def _local_user_id(user: UserDep) -> int:
    if user.source != "local" or user.user_id is None:
        raise ConflictError("API tokens are only for local users")
    return user.user_id


@router.get("/tokens", response_model=list[TokenOut])
def list_tokens(session: SessionDep, user: UserDep) -> list[TokenOut]:
    user_id = _local_user_id(user)
    return [TokenOut.model_validate(t) for t in api_tokens.list_for_user(session, user_id)]


@router.post("/tokens", response_model=TokenCreated, status_code=status.HTTP_201_CREATED)
def create_token(
    body: TokenCreate, session: SessionDep, user: UserDep, actor: ActorDep
) -> TokenCreated:
    owner = users.get(session, _local_user_id(user))
    new = api_tokens.create(session, owner, body.name, expires_days=body.expires_days, actor=actor)
    return TokenCreated.model_validate(
        {**TokenOut.model_validate(new.token).model_dump(), "token": new.raw}
    )


@router.delete("/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_token(token_id: int, session: SessionDep, user: UserDep, actor: ActorDep) -> None:
    api_tokens.revoke(session, token_id, user_id=_local_user_id(user), actor=actor)


# --- gebruikersbeheer (admin) ----------------------------------------------------


@router.get("/users", response_model=list[UserOut], dependencies=ADMIN)
def list_users(session: SessionDep) -> list[UserOut]:
    return [UserOut.model_validate(u) for u in users.list_users(session)]


@router.post(
    "/users", response_model=UserOut, status_code=status.HTTP_201_CREATED, dependencies=ADMIN
)
def create_user(body: UserCreate, session: SessionDep, actor: ActorDep) -> UserOut:
    user = users.create_local(
        session,
        body.username,
        body.password,
        body.roles,
        display_name=body.display_name,
        actor=actor,
    )
    return UserOut.model_validate(user)


@router.get("/users/{user_id}", response_model=UserOut, dependencies=ADMIN)
def get_user(user_id: int, session: SessionDep) -> UserOut:
    return UserOut.model_validate(users.get(session, user_id))


@router.patch("/users/{user_id}", response_model=UserOut, dependencies=ADMIN)
def update_user(user_id: int, body: UserUpdate, session: SessionDep, actor: ActorDep) -> UserOut:
    user = users.update_local(
        session,
        user_id,
        roles=body.roles,
        disabled=body.disabled,
        display_name=body.display_name,
        actor=actor,
    )
    if body.disabled:
        sessions.destroy_for_user(session, user_id)
    return UserOut.model_validate(user)


@router.put("/users/{user_id}/password", status_code=status.HTTP_204_NO_CONTENT, dependencies=ADMIN)
def reset_password(user_id: int, body: PasswordIn, session: SessionDep, actor: ActorDep) -> None:
    users.set_password(session, user_id, body.password, actor=actor)
    sessions.destroy_for_user(session, user_id)


# --- audit log (admin) -------------------------------------------------------------


@router.get("/audit", response_model=list[AuditOut], dependencies=ADMIN)
def list_audit(
    session: SessionDep,
    actor: str | None = None,
    action: str | None = None,
    since: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[AuditOut]:
    entries = audit.list_entries(
        session, actor=actor, action=action, since=since, limit=limit, offset=offset
    )
    return [AuditOut.model_validate(e) for e in entries]
