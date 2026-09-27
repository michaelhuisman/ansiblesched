from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from app.api.schemas import LaunchIn, RunEventOut, RunEventsPage, RunOut
from app.core.db import session_scope
from app.models import RunStatus
from app.services import runs

SessionDep = Annotated[Session, Depends(session_scope)]

# Tot fase 3 (auth) is er geen subject.
ANONYMOUS = "user:anonymous"

router = APIRouter(tags=["runs"])


@router.post(
    "/templates/{template_id}/launch", response_model=RunOut, status_code=status.HTTP_201_CREATED
)
def launch(template_id: int, body: LaunchIn, session: SessionDep) -> RunOut:
    run = runs.launch(
        session, template_id, triggered_by=ANONYMOUS, extra_vars=body.extra_vars, limit=body.limit
    )
    return RunOut.model_validate(run)


@router.get("/runs", response_model=list[RunOut])
def list_runs(
    session: SessionDep,
    template_id: int | None = None,
    status: RunStatus | None = None,
    since: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[RunOut]:
    flt = runs.RunFilter(
        template_id=template_id, status=status, since=since, limit=limit, offset=offset
    )
    return [RunOut.model_validate(r) for r in runs.list_runs(session, flt)]


@router.get("/runs/{run_id}", response_model=RunOut)
def get_run(run_id: int, session: SessionDep) -> RunOut:
    return RunOut.model_validate(runs.get(session, run_id))


@router.get("/runs/{run_id}/events", response_model=RunEventsPage)
def list_events(
    run_id: int,
    session: SessionDep,
    after_seq: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=5000)] = 500,
) -> RunEventsPage:
    items = [
        RunEventOut.model_validate(e)
        for e in runs.list_events(session, run_id, after_seq=after_seq, limit=limit)
    ]
    next_after = items[-1].seq if len(items) == limit else None
    return RunEventsPage(items=items, next_after_seq=next_after)


@router.post("/runs/{run_id}/cancel", response_model=RunOut, status_code=status.HTTP_202_ACCEPTED)
def cancel(run_id: int, session: SessionDep) -> RunOut:
    return RunOut.model_validate(runs.cancel(session, run_id))
