from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.auth import Action, Principal, current_user
from app.core.db import session_scope

SessionDep = Annotated[Session, Depends(session_scope)]
UserDep = Annotated[Principal, Depends(current_user)]


def require(action: Action) -> Callable[[Principal], Principal]:
    def check(user: UserDep) -> Principal:
        if not user.can(action):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"missing permission: {action}")
        return user

    return check
