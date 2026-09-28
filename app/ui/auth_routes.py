"""UI: inloggen (lokaal en OIDC), uitloggen, eigen account, gebruikersbeheer en audit."""

import base64
import json
import logging
import secrets
from typing import Annotated

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import HTMLResponse, Response

from app.api.deps import SessionDep, UserDep
from app.core.auth import (
    SESSION_COOKIE,
    NotAuthenticatedError,
    PermissionDeniedError,
    Principal,
    authenticate,
    client_ip,
    get_oidc_client,
    origin_allowed,
)
from app.core.config import Settings, get_settings
from app.core.oidc import OidcError, PkcePair
from app.models import ROLES
from app.services import api_tokens, audit, sessions, users
from app.services.errors import ServiceError
from app.ui.common import CanManageUsers, actor, redirect, render

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ui", include_in_schema=False)

OIDC_COOKIE = "sched_oidc"
OIDC_COOKIE_MAX_AGE = 600
DEFAULT_NEXT = "/ui/runs"


def safe_next(value: str | None) -> str:
    """Alleen relatieve paden binnen de UI (geen open redirect)."""
    if value and value.startswith("/ui") and not value.startswith("//") and "\\" not in value:
        return value
    return DEFAULT_NEXT


def _set_session_cookie(response: Response, raw: str, settings: Settings) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        raw,
        max_age=settings.session_max_s,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )


def _redirect_uri(settings: Settings) -> str:
    return f"{settings.public_url.rstrip('/')}/ui/auth/callback"


# --- inloggen / uitloggen --------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: Annotated[str | None, Query()] = None) -> Response:
    try:
        if await authenticate(request) is not None:
            return redirect(safe_next(next))
    except (NotAuthenticatedError, PermissionDeniedError):
        pass  # ongeldige token of sessie: gewoon het loginscherm tonen
    return render(request, "login.html", None, next=safe_next(next), error=None)


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    session: SessionDep,
    username: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = DEFAULT_NEXT,
) -> Response:
    settings = get_settings()
    if not settings.auth_local_enabled:
        raise PermissionDeniedError("local login is disabled")
    if not origin_allowed(request):
        raise PermissionDeniedError("cross-origin request refused")
    try:
        user = users.authenticate(
            session,
            username,
            password,
            max_failures=settings.login_max_failures,
            lockout_s=settings.login_lockout_s,
            ip=client_ip(request),
        )
    except users.AuthenticationError:
        return render(
            request,
            "login.html",
            None,
            code=401,
            next=safe_next(next),
            username=username,
            error=(
                "Onjuiste gebruikersnaam of wachtwoord, of het account is (tijdelijk) geblokkeerd."
            ),
        )
    new = sessions.create(session, user.id, oidc_roles=[], max_s=settings.session_max_s)
    response = redirect(safe_next(next))
    _set_session_cookie(response, new.raw_token, settings)
    return response


@router.post("/logout", response_class=HTMLResponse)
def logout(request: Request, session: SessionDep, user: UserDep) -> Response:
    raw = request.cookies.get(SESSION_COOKIE)
    if raw:
        sessions.destroy(session, raw)
    audit.record(session, actor(request, user), "logout")
    session.commit()
    response = redirect("/ui/login")
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


# --- OIDC ------------------------------------------------------------------------


def _encode_state(data: dict[str, str]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode()


def _decode_state(value: str | None) -> dict[str, str] | None:
    if not value:
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(value.encode()))
    except (ValueError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


@router.get("/auth/oidc/start")
def oidc_start(next: Annotated[str | None, Query()] = None) -> Response:
    client = get_oidc_client()
    if client is None:
        raise PermissionDeniedError("OIDC login is not configured")
    settings = get_settings()
    state, nonce, pkce = secrets.token_urlsafe(24), secrets.token_urlsafe(24), PkcePair.new()
    url = client.authorize_url(
        redirect_uri=_redirect_uri(settings), state=state, nonce=nonce, pkce=pkce
    )
    response = redirect(url)
    # Lax: de cookie gaat mee met de top-level redirect terug van de IdP.
    response.set_cookie(
        OIDC_COOKIE,
        _encode_state(
            {"state": state, "nonce": nonce, "verifier": pkce.verifier, "next": safe_next(next)}
        ),
        max_age=OIDC_COOKIE_MAX_AGE,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/ui/auth",
    )
    return response


def _oidc_failed(request: Request, reason: str, session: SessionDep) -> Response:
    audit.record(
        session,
        audit.Actor("oidc:unknown", client_ip(request)),
        "login.failed",
        details={"reason": reason},
    )
    session.commit()
    response = render(
        request,
        "login.html",
        None,
        code=401,
        next=DEFAULT_NEXT,
        error="Inloggen via Keycloak is mislukt. Probeer het opnieuw.",
    )
    response.delete_cookie(OIDC_COOKIE, path="/ui/auth")
    return response


@router.get("/auth/callback")
def oidc_callback(
    request: Request,
    session: SessionDep,
    code: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
) -> Response:
    client = get_oidc_client()
    if client is None:
        raise PermissionDeniedError("OIDC login is not configured")
    settings = get_settings()
    saved = _decode_state(request.cookies.get(OIDC_COOKIE))
    if error or not code or not state or saved is None:
        return _oidc_failed(request, f"callback error: {error or 'missing state'}", session)
    if not secrets.compare_digest(state, saved.get("state", "")):
        return _oidc_failed(request, "state mismatch", session)
    try:
        tokens = client.exchange_code(
            code=code, redirect_uri=_redirect_uri(settings), verifier=saved.get("verifier", "")
        )
        id_claims = client.validate_id_token(tokens["id_token"], saved.get("nonce", ""))
        access_claims = client.validate_access_token(tokens["access_token"])
    except (OidcError, KeyError) as exc:
        log.info("oidc login failed", extra={"reason": str(exc)})
        return _oidc_failed(request, str(exc), session)
    if id_claims["sub"] != access_claims["sub"]:
        return _oidc_failed(request, "subject mismatch", session)

    roles = client.roles(access_claims)
    user = users.upsert_oidc(
        session,
        id_claims["sub"],
        username=id_claims.get("preferred_username"),
        display_name=id_claims.get("name") or id_claims.get("preferred_username"),
        email=id_claims.get("email"),
    )
    who = audit.Actor(f"user:oidc:{user.username}", client_ip(request))
    if user.disabled or not roles:
        reason = "disabled" if user.disabled else "no roles"
        audit.record(session, who, "login.failed", "user", user.id, {"reason": reason})
        session.commit()
        return render(
            request,
            "login.html",
            None,
            code=403,
            next=DEFAULT_NEXT,
            error="Je account heeft geen toegang tot ansible-scheduler (geen rol toegewezen).",
        )
    audit.record(session, who, "login", "user", user.id, {"method": "oidc", "roles": roles})
    session.commit()
    new = sessions.create(session, user.id, oidc_roles=roles, max_s=settings.session_max_s)
    response = redirect(safe_next(saved.get("next")))
    _set_session_cookie(response, new.raw_token, settings)
    response.delete_cookie(OIDC_COOKIE, path="/ui/auth")
    return response


# --- eigen account -----------------------------------------------------------------


def _account(
    request: Request, session: SessionDep, user: Principal, code: int = 200, **ctx: object
) -> HTMLResponse:
    tokens = (
        api_tokens.list_for_user(session, user.user_id)
        if user.source == "local" and user.user_id is not None
        else []
    )
    return render(request, "account.html", user, code=code, tokens=tokens, **ctx)


@router.get("/account", response_class=HTMLResponse)
def account(request: Request, session: SessionDep, user: UserDep) -> HTMLResponse:
    return _account(request, session, user)


@router.post("/account/password", response_class=HTMLResponse)
def change_password(
    request: Request,
    session: SessionDep,
    user: UserDep,
    current: Annotated[str, Form()] = "",
    new: Annotated[str, Form()] = "",
) -> Response:
    if user.source != "local" or user.user_id is None:
        raise PermissionDeniedError("only local users have a password here")
    me = users.get(session, user.user_id)
    if not users.verify_password(me, current):
        return _account(request, session, user, code=400, pw_error="Huidig wachtwoord klopt niet.")
    try:
        users.set_password(session, me.id, new, actor=actor(request, user))
    except ServiceError as exc:
        return _account(request, session, user, code=400, pw_error=str(exc))
    sessions.destroy_for_user(session, me.id)
    response = redirect("/ui/login")
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.post("/account/tokens", response_class=HTMLResponse)
def create_token(
    request: Request,
    session: SessionDep,
    user: UserDep,
    name: Annotated[str, Form()] = "",
    expires_days: Annotated[str, Form()] = "90",
) -> HTMLResponse:
    if user.source != "local" or user.user_id is None:
        raise PermissionDeniedError("API tokens are only for local users")
    days = int(expires_days) if expires_days.isdigit() else None
    try:
        new = api_tokens.create(
            session,
            users.get(session, user.user_id),
            name,
            expires_days=days,
            actor=actor(request, user),
        )
    except ServiceError as exc:
        return _account(request, session, user, code=400, token_error=str(exc))
    return _account(request, session, user, new_token=new.raw, new_token_name=new.token.name)


@router.post("/account/tokens/{token_id}/revoke", response_class=HTMLResponse)
def revoke_token(request: Request, token_id: int, session: SessionDep, user: UserDep) -> Response:
    if user.user_id is None:
        raise PermissionDeniedError("no local user")
    api_tokens.revoke(session, token_id, user_id=user.user_id, actor=actor(request, user))
    return redirect("/ui/account")


# --- gebruikersbeheer (admin) --------------------------------------------------------


@router.get("/users", response_class=HTMLResponse)
def users_page(request: Request, session: SessionDep, user: CanManageUsers) -> HTMLResponse:
    return render(request, "users.html", user, users=users.list_users(session), roles=ROLES)


@router.post("/users", response_class=HTMLResponse)
async def user_create(request: Request, session: SessionDep, user: CanManageUsers) -> Response:
    form = await request.form()
    roles = [r for r in form.getlist("roles") if isinstance(r, str)]
    try:
        users.create_local(
            session,
            str(form.get("username", "")),
            str(form.get("password", "")),
            roles,
            display_name=str(form.get("display_name", "")) or None,
            actor=actor(request, user),
        )
    except ServiceError as exc:
        return render(
            request,
            "users.html",
            user,
            code=400,
            users=users.list_users(session),
            roles=ROLES,
            create_error=str(exc),
            form=dict(form),
        )
    return redirect("/ui/users")


@router.post("/users/{user_id}", response_class=HTMLResponse)
async def user_update(
    request: Request, user_id: int, session: SessionDep, user: CanManageUsers
) -> Response:
    form = await request.form()
    roles = [r for r in form.getlist("roles") if isinstance(r, str)]
    disabled = form.get("disabled") == "on"
    if user.user_id == user_id and (disabled or "admin" not in roles):
        return render(
            request,
            "users.html",
            user,
            code=400,
            users=users.list_users(session),
            roles=ROLES,
            row_error={user_id: "Je kunt je eigen admin-rol of account niet uitschakelen."},
        )
    try:
        users.update_local(
            session, user_id, roles=roles, disabled=disabled, actor=actor(request, user)
        )
        if disabled:
            sessions.destroy_for_user(session, user_id)
        new_password = str(form.get("new_password", ""))
        if new_password:
            users.set_password(session, user_id, new_password, actor=actor(request, user))
            sessions.destroy_for_user(session, user_id)
    except ServiceError as exc:
        return render(
            request,
            "users.html",
            user,
            code=400,
            users=users.list_users(session),
            roles=ROLES,
            row_error={user_id: str(exc)},
        )
    return redirect("/ui/users")


# --- audit log (admin) ----------------------------------------------------------------


@router.get("/audit", response_class=HTMLResponse)
def audit_page(
    request: Request,
    session: SessionDep,
    user: CanManageUsers,
    actor_filter: Annotated[str | None, Query(alias="actor")] = None,
    action: Annotated[str | None, Query()] = None,
) -> HTMLResponse:
    entries = audit.list_entries(
        session, actor=actor_filter or None, action=action or None, limit=300
    )
    return render(
        request,
        "audit.html",
        user,
        entries=entries,
        actor_filter=actor_filter or "",
        action=action or "",
    )
