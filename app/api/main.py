import hmac
import logging
from urllib.parse import quote

from fastapi import APIRouter, FastAPI, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.api.routers import auth, config, runs
from app.core.auth import NotAuthenticatedError, PermissionDeniedError
from app.core.config import get_settings
from app.core.db import get_engine, get_sessionmaker
from app.services.errors import ConflictError, InvalidReferenceError, NotFoundError, ServiceError
from app.services.metrics import build_registry
from app.ui import auth_routes as ui_auth
from app.ui import config_routes as ui_config
from app.ui import routes as ui
from app.ui.common import STATIC_DIR

log = logging.getLogger(__name__)

_STATUS_FOR_ERROR: dict[type[Exception], int] = {
    NotFoundError: status.HTTP_404_NOT_FOUND,
    ConflictError: status.HTTP_409_CONFLICT,
    InvalidReferenceError: status.HTTP_422_UNPROCESSABLE_CONTENT,
}


async def _service_error(_request: Request, exc: Exception) -> JSONResponse:
    code = _STATUS_FOR_ERROR.get(type(exc), status.HTTP_400_BAD_REQUEST)
    return JSONResponse(status_code=code, content={"detail": str(exc)})


def _is_ui(request: Request) -> bool:
    return request.url.path.startswith("/ui")


async def _not_authenticated(request: Request, exc: Exception) -> Response:
    if _is_ui(request):
        target = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        login = f"/ui/login?next={quote(target, safe='')}"
        if request.headers.get("HX-Request") == "true":
            return Response(status_code=200, headers={"HX-Redirect": login})
        return RedirectResponse(login, status_code=status.HTTP_303_SEE_OTHER)
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"detail": str(exc)},
        headers={"WWW-Authenticate": "Bearer"},
    )


async def _permission_denied(request: Request, exc: Exception) -> Response:
    if _is_ui(request):
        return HTMLResponse(
            '<!doctype html><html lang="en"><title>Access denied</title>'
            '<link rel="stylesheet" href="/ui/static/app.css">'
            '<main><h1>Access denied</h1><p class="muted">You do not have the required role.</p>'
            '<p><a href="/ui/runs">Back</a></p></main>',
            status_code=status.HTTP_403_FORBIDDEN,
        )
    return JSONResponse(status_code=status.HTTP_403_FORBIDDEN, content={"detail": str(exc)})


def create_app() -> FastAPI:
    app = FastAPI(title="lamplighter", version="0.1.0")
    app.add_exception_handler(ServiceError, _service_error)
    app.add_exception_handler(NotAuthenticatedError, _not_authenticated)
    app.add_exception_handler(PermissionDeniedError, _permission_denied)

    v1 = APIRouter(prefix="/api/v1")
    for router in config.routers:
        v1.include_router(router)
    v1.include_router(runs.router)
    v1.include_router(auth.router)
    app.include_router(v1)
    app.include_router(ui.router)
    app.include_router(ui_auth.router)
    app.include_router(ui_config.router)
    app.mount("/ui/static", StaticFiles(directory=STATIC_DIR), name="static")
    registry = build_registry(get_sessionmaker())

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/ui/runs")

    @app.get("/metrics", include_in_schema=False)
    def metrics(request: Request) -> Response:
        token = get_settings().metrics_token
        if token is not None:
            supplied = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            if not hmac.compare_digest(supplied.encode(), token.get_secret_value().encode()):
                return Response(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    headers={"WWW-Authenticate": "Bearer"},
                )
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
