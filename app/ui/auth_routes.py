"""UI: login (local and OIDC), logout, own account, user management and audit."""

import base64
import json
import logging
import secrets
from typing import Annotated, Any

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
from app.models import ROLES, User
from app.services import api_tokens, audit, sessions, users
from app.services.errors import ServiceError
from app.ui.common import CanManageUsers, actor, redirect, render

log = logging.getLogger(__name__)

router = APIRouter(prefix="/ui", include_in_schema=False)

OIDC_COOKIE = "lamplighter_oidc"
OIDC_COOKIE_MAX_AGE = 600
DEFAULT_NEXT = "/ui/runs"

LOGIN_FAILED = "Invalid username or password, or the account is (temporarily) locked."
PASSWORDS_DIFFER = "The passwords do not match."


def safe_next(value: str | None) -> str:
    """Only relative paths within the UI (no open redirect)."""
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


# --- login / logout --------------------------------------------------------------


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: Annotated[str | None, Query()] = None) -> Response:
    try:
        if await authenticate(request) is not None:
            return redirect(safe_next(next))
    except (NotAuthenticatedError, PermissionDeniedError):
        pass  # invalid token or session: just show the login screen
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
            error=LOGIN_FAILED,
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
    # Lax: the cookie is sent along with the top-level redirect back from the IdP.
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
        error="Sign-in with Keycloak failed. Please try again.",
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
            error="Your account has no access to lamplighter (no role assigned).",
        )
    audit.record(session, who, "login", "user", user.id, {"method": "oidc", "roles": roles})
    session.commit()
    new = sessions.create(session, user.id, oidc_roles=roles, max_s=settings.session_max_s)
    response = redirect(safe_next(saved.get("next")))
    _set_session_cookie(response, new.raw_token, settings)
    response.delete_cookie(OIDC_COOKIE, path="/ui/auth")
    return response


# --- own account -------------------------------------------------------------------


def _local_user_id(user: Principal) -> int:
    if user.source != "local" or user.user_id is None:
        raise PermissionDeniedError("only local users can do this")
    return user.user_id


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


def _own_password_form(
    request: Request, user: Principal, code: int = 200, error: str | None = None
) -> HTMLResponse:
    return render(
        request,
        "password_form.html",
        user,
        code=code,
        title="Change password",
        subject=None,
        action="/ui/account/password",
        back="/ui/account",
        ask_current=True,
        submit="Change password",
        note="You will be signed out everywhere and need to sign in again.",
        error=error,
    )


@router.get("/account/password", response_class=HTMLResponse)
def change_password_page(request: Request, user: UserDep) -> HTMLResponse:
    _local_user_id(user)
    return _own_password_form(request, user)


@router.post("/account/password", response_class=HTMLResponse)
def change_password(
    request: Request,
    session: SessionDep,
    user: UserDep,
    current: Annotated[str, Form()] = "",
    new: Annotated[str, Form()] = "",
    confirm: Annotated[str, Form()] = "",
) -> Response:
    me = users.get(session, _local_user_id(user))
    if not users.verify_password(me, current):
        return _own_password_form(request, user, 400, "The current password is incorrect.")
    if new != confirm:
        return _own_password_form(request, user, 400, PASSWORDS_DIFFER)
    try:
        users.set_password(session, me.id, new, actor=actor(request, user))
    except ServiceError as exc:
        return _own_password_form(request, user, 400, str(exc))
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
    user_id = _local_user_id(user)
    days = int(expires_days) if expires_days.isdigit() else None
    try:
        new = api_tokens.create(
            session,
            users.get(session, user_id),
            name,
            expires_days=days,
            actor=actor(request, user),
        )
    except ServiceError as exc:
        return _account(request, session, user, code=400, token_error=str(exc))
    return _account(request, session, user, new_token=new.raw, new_token_name=new.token.name)


@router.post("/account/tokens/{token_id}/revoke", response_class=HTMLResponse)
def revoke_token(request: Request, token_id: int, session: SessionDep, user: UserDep) -> Response:
    api_tokens.revoke(session, token_id, user_id=_local_user_id(user), actor=actor(request, user))
    return redirect("/ui/account")


# --- user management (admin) ---------------------------------------------------------


def _user_form(
    request: Request,
    user: Principal,
    item: User | None,
    form: dict[str, Any],
    code: int = 200,
    error: str | None = None,
) -> HTMLResponse:
    return render(
        request,
        "user_form.html",
        user,
        code=code,
        item=item,
        editing=item is not None,
        form=form,
        roles=ROLES,
        error=error,
    )


def _roles(values: list[Any]) -> list[str]:
    return [r for r in values if isinstance(r, str)]


@router.get("/users", response_class=HTMLResponse)
def users_page(request: Request, session: SessionDep, user: CanManageUsers) -> HTMLResponse:
    return render(request, "users.html", user, users=users.list_users(session))


@router.get("/users/new", response_class=HTMLResponse)
def user_new(request: Request, user: CanManageUsers) -> HTMLResponse:
    return _user_form(request, user, None, {"roles": ["viewer"]})


@router.post("/users", response_class=HTMLResponse)
async def user_create(request: Request, session: SessionDep, user: CanManageUsers) -> Response:
    form = await request.form()
    username = str(form.get("username", ""))
    display_name = str(form.get("display_name", ""))
    roles = _roles(form.getlist("roles"))
    values = {"username": username, "display_name": display_name, "roles": roles}
    password = str(form.get("password", ""))
    if password != str(form.get("password_confirm", "")):
        return _user_form(request, user, None, values, 400, PASSWORDS_DIFFER)
    try:
        users.create_local(
            session,
            username,
            password,
            roles,
            display_name=display_name or None,
            actor=actor(request, user),
        )
    except ServiceError as exc:
        return _user_form(request, user, None, values, 400, str(exc))
    return redirect("/ui/users")


@router.get("/users/{user_id}/edit", response_class=HTMLResponse)
def user_edit(
    request: Request, user_id: int, session: SessionDep, user: CanManageUsers
) -> HTMLResponse:
    item = users.get(session, user_id)
    form = {"display_name": item.display_name, "roles": item.roles, "disabled": item.disabled}
    return _user_form(request, user, item, form)


@router.post("/users/{user_id}", response_class=HTMLResponse)
async def user_update(
    request: Request, user_id: int, session: SessionDep, user: CanManageUsers
) -> Response:
    item = users.get(session, user_id)
    form = await request.form()
    display_name = str(form.get("display_name", ""))
    roles = _roles(form.getlist("roles"))
    disabled = form.get("disabled") == "on"
    values = {"display_name": display_name, "roles": roles, "disabled": disabled}
    if user.user_id == user_id and (disabled or "admin" not in roles):
        return _user_form(
            request,
            user,
            item,
            values,
            400,
            "You cannot remove your own admin role or disable yourself.",
        )
    try:
        users.update_local(
            session,
            user_id,
            roles=roles,
            disabled=disabled,
            display_name=display_name,
            actor=actor(request, user),
        )
    except ServiceError as exc:
        return _user_form(request, user, item, values, 400, str(exc))
    if disabled:
        sessions.destroy_for_user(session, user_id)
    return redirect("/ui/users")


def _reset_password_form(
    request: Request, user: Principal, item: User, code: int = 200, error: str | None = None
) -> HTMLResponse:
    return render(
        request,
        "password_form.html",
        user,
        code=code,
        title="Reset password",
        subject=item.display_name or item.username,
        action=f"/ui/users/{item.id}/password",
        back=f"/ui/users/{item.id}/edit",
        ask_current=False,
        submit="Reset password",
        note="The user is signed out everywhere. Existing API tokens stay valid.",
        error=error,
    )


@router.get("/users/{user_id}/password", response_class=HTMLResponse)
def user_password_page(
    request: Request, user_id: int, session: SessionDep, user: CanManageUsers
) -> HTMLResponse:
    item = users.get(session, user_id)
    if item.source != "local":
        raise PermissionDeniedError("OIDC users have no local password")
    return _reset_password_form(request, user, item)


@router.post("/users/{user_id}/password", response_class=HTMLResponse)
def user_password_reset(
    request: Request,
    user_id: int,
    session: SessionDep,
    user: CanManageUsers,
    new: Annotated[str, Form()] = "",
    confirm: Annotated[str, Form()] = "",
) -> Response:
    item = users.get(session, user_id)
    if new != confirm:
        return _reset_password_form(request, user, item, 400, PASSWORDS_DIFFER)
    try:
        users.set_password(session, user_id, new, actor=actor(request, user))
    except ServiceError as exc:
        return _reset_password_form(request, user, item, 400, str(exc))
    sessions.destroy_for_user(session, user_id)
    return redirect(f"/ui/users/{user_id}/edit")


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
