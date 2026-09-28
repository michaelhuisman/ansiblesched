from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.core.auth import Action, PermissionDeniedError, Principal, client_ip, current_user
from app.core.db import session_scope
from app.services.audit import Actor

SessionDep = Annotated[Session, Depends(session_scope)]
UserDep = Annotated[Principal, Depends(current_user)]


def require(action: Action) -> Callable[[Principal], Principal]:
    def check(user: UserDep) -> Principal:
        if not user.can(action):
            raise PermissionDeniedError(f"missing permission: {action}")
        return user

    return check


def get_actor(request: Request, user: UserDep) -> Actor:
    return Actor(user.triggered_by, client_ip(request))


ActorDep = Annotated[Actor, Depends(get_actor)]
