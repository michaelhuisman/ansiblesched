import logging

from fastapi import APIRouter, FastAPI, Request, status
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api.routers import config, runs
from app.core.db import get_engine, get_sessionmaker
from app.services.errors import ConflictError, InvalidReferenceError, NotFoundError, ServiceError
from app.services.metrics import build_registry
from app.ui import routes as ui

log = logging.getLogger(__name__)

_STATUS_FOR_ERROR: dict[type[Exception], int] = {
    NotFoundError: status.HTTP_404_NOT_FOUND,
    ConflictError: status.HTTP_409_CONFLICT,
    InvalidReferenceError: status.HTTP_422_UNPROCESSABLE_CONTENT,
}


async def _service_error(_request: Request, exc: Exception) -> JSONResponse:
    code = _STATUS_FOR_ERROR.get(type(exc), status.HTTP_400_BAD_REQUEST)
    return JSONResponse(status_code=code, content={"detail": str(exc)})


def create_app() -> FastAPI:
    app = FastAPI(title="ansible-scheduler", version="0.1.0")
    app.add_exception_handler(ServiceError, _service_error)

    v1 = APIRouter(prefix="/api/v1")
    for router in config.routers:
        v1.include_router(router)
    v1.include_router(runs.router)
    app.include_router(v1)
    app.include_router(ui.router)
    app.mount("/ui/static", StaticFiles(directory=ui.STATIC_DIR), name="static")
    registry = build_registry(get_sessionmaker())

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/ui/runs")

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    def readyz() -> JSONResponse:
        try:
            with get_engine().connect() as conn:
                conn.execute(text("SELECT 1"))
        except SQLAlchemyError:
            log.warning("readiness check failed", exc_info=True)
            return JSONResponse(status_code=503, content={"status": "unavailable"})
        return JSONResponse(content={"status": "ok"})

    return app
