"""Fase 4a: authenticatie, RBAC, CSRF, lockout, API-tokens, audit en OIDC."""

import os
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import pytest
from sqlalchemy import select, update

from app.core.db import get_sessionmaker
from app.models import ApiToken, AuditEntry
from tests.integration.conftest import (
    API_URL,
    SECRETS_DIR,
    Env,
    LocalUser,
    ensure_local_user,
    login_ui,
    post,
    unique,
)

KEYCLOAK = os.environ.get("SCHED_IT_KEYCLOAK", "http://127.0.0.1:8080")
KC_TOKEN_URL = f"{KEYCLOAK}/realms/scheduler/protocol/openid-connect/token"


def kc_password(user: str) -> str:
    return (Path(SECRETS_DIR) / "keycloak" / user).read_text().strip()


def kc_token(user: str) -> str:
    resp = httpx.post(
        KC_TOKEN_URL,
        data={
            "grant_type": "password",
            "client_id": "scheduler-tests",
            "username": user,
            "password": kc_password(user),
        },
        timeout=10,
    )
    resp.raise_for_status()
    token: str = resp.json()["access_token"]
    return token


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def local_users() -> dict[str, LocalUser]:
    return {role: ensure_local_user(f"it-{role}", [role]) for role in ("viewer", "operator")}


@pytest.fixture(scope="module")
def finished_run(env: Env) -> dict[str, Any]:
    return env.wait(env.launch(env.template("ping.yml"))["id"])


# --- RBAC-matrix -------------------------------------------------------------------

Call = Callable[[httpx.Client, dict[str, Any]], httpx.Response]

CALLS: dict[str, tuple[str, Call]] = {
    "list runs": ("read", lambda c, ctx: c.get("/runs", params={"limit": 1})),
    "get template": ("read", lambda c, ctx: c.get(f"/templates/{ctx['template']['id']}")),
    "launch": (
        "launch",
        lambda c, ctx: c.post(f"/templates/{ctx['template']['id']}/launch", json={}),
    ),
    # Een afgeronde run annuleren geeft 409 als je het mag, 403 als je het niet mag.
    "cancel": ("cancel", lambda c, ctx: c.post(f"/runs/{ctx['run']['id']}/cancel")),
    "create project": (
        "configure",
        lambda c, ctx: c.post("/projects", json={"name": unique("rbac"), "git_url": "file:///x"}),
    ),
    "list users": ("manage_users", lambda c, ctx: c.get("/users")),
    "audit": ("manage_users", lambda c, ctx: c.get("/audit", params={"limit": 1})),
}

ALLOWED = {
    "viewer": {"read"},
    "operator": {"read", "launch", "cancel"},
    "admin": {"read", "launch", "cancel", "configure", "manage_users"},
    "none": set(),
}


def identities(
    local_users: dict[str, LocalUser], admin: LocalUser
) -> dict[str, tuple[str, dict[str, str]]]:
    return {
        "local viewer": ("viewer", local_users["viewer"].headers),
        "local operator": ("operator", local_users["operator"].headers),
        "local admin": ("admin", admin.headers),
        "kc viewer": ("viewer", bearer(kc_token("kc-viewer"))),
        "kc operator": ("operator", bearer(kc_token("kc-operator"))),
        "kc admin": ("admin", bearer(kc_token("kc-admin"))),
        "kc no role": ("none", bearer(kc_token("kc-norole"))),
    }


def test_rbac_matrix(
    env: Env, admin: LocalUser, local_users: dict[str, LocalUser], finished_run: dict[str, Any]
) -> None:
    ctx = {"template": env.template("ping.yml"), "run": finished_run}
    failures = []
    launched: list[int] = []
    for who, (role, headers) in identities(local_users, admin).items():
        with httpx.Client(base_url=f"{API_URL}/api/v1", headers=headers, timeout=10) as c:
            for name, (action, call) in CALLS.items():
                resp = call(c, ctx)
                allowed = action in ALLOWED[role]
                if allowed and resp.status_code in (401, 403):
                    failures.append(f"{who} / {name}: expected access, got {resp.status_code}")
                if not allowed and resp.status_code != 403:
                    failures.append(f"{who} / {name}: expected 403, got {resp.status_code}")
                if name == "launch" and resp.status_code == 201:
                    launched.append(resp.json()["id"])
    assert not failures, "\n".join(failures)
    for run_id in launched:
        env.wait(run_id)


def test_anonymous_gets_401(finished_run: dict[str, Any]) -> None:
    with httpx.Client(base_url=f"{API_URL}/api/v1", timeout=10) as c:
        for path in ("/runs", f"/runs/{finished_run['id']}", "/templates", "/me", "/users"):
            resp = c.get(path)
            assert resp.status_code == 401, path
            assert resp.headers["www-authenticate"] == "Bearer"
    # Open endpoints
    assert httpx.get(f"{API_URL}/healthz").status_code == 200
    assert httpx.get(f"{API_URL}/metrics").status_code == 200


@pytest.mark.parametrize(
    "token",
    ["abc.def.ghi", "not-a-jwt", "sched_doesnotexist", "eyJhbGciOiJub25lIn0.e30."],
)
def test_invalid_tokens_get_401(token: str) -> None:
    resp = httpx.get(f"{API_URL}/api/v1/runs", headers=bearer(token))
    assert resp.status_code == 401


def test_tampered_keycloak_token_gets_401() -> None:
    header, payload, sig = kc_token("kc-viewer").split(".")
    # Andere payload met de oorspronkelijke handtekening.
    forged = ".".join([header, payload[:-4] + ("AAAA" if payload[-4:] != "AAAA" else "BBBB"), sig])
    assert httpx.get(f"{API_URL}/api/v1/runs", headers=bearer(forged)).status_code == 401


def test_me_reports_identity(admin: LocalUser) -> None:
    me = httpx.get(f"{API_URL}/api/v1/me", headers=admin.headers).json()
    assert me == {
        "subject": "local:it-admin",
        "display_name": None,
        "source": "local",
        "roles": ["admin"],
        "via": "token",
    }
    kc = httpx.get(f"{API_URL}/api/v1/me", headers=bearer(kc_token("kc-operator"))).json()
    assert kc["source"] == "oidc"
    assert kc["roles"] == ["operator"]
    assert kc["subject"].startswith("oidc:")


def test_triggered_by_and_audit_for_launch(env: Env) -> None:
    template = env.template("ping.yml")
    run = env.launch(template)
    assert run["triggered_by"] == "user:local:it-admin"
    kc = httpx.post(
        f"{API_URL}/api/v1/templates/{template['id']}/launch",
        headers=bearer(kc_token("kc-operator")),
        json={},
    ).json()
    assert kc["triggered_by"].startswith("user:oidc:")
    entries = env.api.get("/audit", params={"action": "run.launch", "limit": 20}).json()
    ids = {e["object_id"] for e in entries}
    assert {str(run["id"]), str(kc["id"])} <= ids
    env.wait(run["id"])
    env.wait(kc["id"])


def test_audit_records_config_changes_without_values(env: Env) -> None:
    name = unique("audit")
    project = post(env.api, "/projects", {"name": name, "git_url": "file:///secret-looking-url"})
    env.api.put(f"/projects/{project['id']}", json={"name": name, "git_url": "file:///y"})
    env.api.delete(f"/projects/{project['id']}")
    entries = env.api.get("/audit", params={"action": "projects.", "limit": 20}).json()
    mine = [e for e in entries if e["object_id"] == str(project["id"])]
    assert [e["action"] for e in mine] == ["projects.delete", "projects.update", "projects.create"]
    assert mine[1]["details"] == {"name": name, "changed": ["git_url"]}
    assert all(e["actor"] == "user:local:it-admin" for e in mine)
    assert "secret-looking-url" not in str(entries)


# --- API-tokens --------------------------------------------------------------------


def test_api_token_lifecycle(env: Env) -> None:
    user = ensure_local_user(unique("it-tok"), ["viewer"])
    c = httpx.Client(base_url=f"{API_URL}/api/v1", headers=user.headers, timeout=10)
    created = c.post("/tokens", json={"name": "script", "expires_days": 7})
    assert created.status_code == 201
    raw = created.json()["token"]
    assert raw.startswith("sched_")
    listed = c.get("/tokens").json()
    assert any(t["id"] == created.json()["id"] for t in listed)
    assert all("token" not in t for t in listed)  # alleen bij aanmaken

    new = httpx.Client(base_url=f"{API_URL}/api/v1", headers=bearer(raw), timeout=10)
    assert new.get("/runs", params={"limit": 1}).status_code == 200

    # Verlopen
    with get_sessionmaker()() as s:
        s.execute(
            update(ApiToken)
            .where(ApiToken.id == created.json()["id"])
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        s.commit()
    assert new.get("/runs").status_code == 401

    # Intrekken
    other = c.post("/tokens", json={"name": "other"}).json()
    assert c.delete(f"/tokens/{other['id']}").status_code == 204
    assert httpx.get(f"{API_URL}/api/v1/runs", headers=bearer(other["token"])).status_code == 401


def test_oidc_users_cannot_create_api_tokens() -> None:
    resp = httpx.post(
        f"{API_URL}/api/v1/tokens",
        headers=bearer(kc_token("kc-admin")),
        json={"name": "x"},
    )
    assert resp.status_code == 409


def test_disabling_user_revokes_access(env: Env) -> None:
    user = ensure_local_user(unique("it-dis"), ["viewer"])
    ui = login_ui(user.username, user.password)
    me = env.api.get("/users").json()
    user_id = next(u["id"] for u in me if u["username"] == user.username)
    assert env.api.patch(f"/users/{user_id}", json={"disabled": True}).status_code == 200
    assert httpx.get(f"{API_URL}/api/v1/runs", headers=user.headers).status_code == 401
    assert ui.get("/runs").status_code == 303  # sessie weg, terug naar login


# --- gebruikersbeheer via API ---------------------------------------------------------


def test_user_management_api(env: Env) -> None:
    name = unique("it-new")
    weak = env.api.post("/users", json={"username": name, "password": "short", "roles": []})
    assert weak.status_code == 422
    created = post(
        env.api,
        "/users",
        {"username": name, "password": "a-long-enough-password", "roles": ["viewer"]},
    )
    assert "password" not in str(created)
    assert "password_hash" not in created
    dup = env.api.post("/users", json={"username": name, "password": "x" * 12, "roles": []})
    assert dup.status_code == 409
    upd = env.api.patch(f"/users/{created['id']}", json={"roles": ["operator", "viewer"]})
    assert upd.json()["roles"] == ["operator", "viewer"]
    bad_role = env.api.patch(f"/users/{created['id']}", json={"roles": ["root"]})
    assert bad_role.status_code == 422


# --- UI: login, lockout, CSRF, logout -------------------------------------------------


def test_ui_requires_login() -> None:
    resp = httpx.get(f"{API_URL}/ui/templates", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/ui/login?next=%2Fui%2Ftemplates"
    htmx = httpx.get(f"{API_URL}/ui/runs", headers={"HX-Request": "true"}, follow_redirects=False)
    assert htmx.headers["HX-Redirect"].startswith("/ui/login")


def test_ui_login_and_logout(local_users: dict[str, LocalUser]) -> None:
    user = local_users["viewer"]
    ui = login_ui(user.username, user.password)
    cookie = ui.cookies.get("sched_session")
    assert cookie
    page = ui.get("/runs")
    assert page.status_code == 200
    assert "it-viewer" in page.text
    assert "New template" not in ui.get("/templates").text  # viewer: geen beheerknoppen
    assert ui.get("/templates/new").status_code == 403

    assert ui.post("/logout").status_code == 303
    again = httpx.get(
        f"{API_URL}/ui/runs", cookies={"sched_session": cookie}, follow_redirects=False
    )
    assert again.status_code == 303


def test_ui_login_wrong_password_and_lockout() -> None:
    user = ensure_local_user(unique("it-lock"), ["viewer"])
    c = httpx.Client(base_url=f"{API_URL}/ui", follow_redirects=False, timeout=10)
    for _ in range(5):
        resp = c.post("/login", data={"username": user.username, "password": "wrong-password!"})
        assert resp.status_code == 401
        assert "Invalid username or password" in resp.text
    locked = c.post("/login", data={"username": user.username, "password": user.password})
    assert locked.status_code == 401, "account should be locked"
    unknown = c.post("/login", data={"username": unique("nobody"), "password": "whatever-123"})
    assert unknown.status_code == 401
    assert (
        unknown.text.count("Invalid username or password") == 1
    )  # zelfde melding: geen enumeratie

    with get_sessionmaker()() as s:
        reasons = [
            e.details.get("reason")
            for e in s.scalars(
                select(AuditEntry)
                .where(AuditEntry.actor == f"user:local:{user.username}")
                .order_by(AuditEntry.id)
            )
        ]
    assert reasons.count("password") == 4
    assert "password, now locked" in reasons
    assert reasons[-1] == "locked"


def test_csrf_is_enforced(admin: LocalUser, env: Env) -> None:
    ui = login_ui(admin.username, admin.password)
    token = ui.headers.pop("X-CSRF-Token")
    template = env.template("ping.yml")
    path = f"/templates/{template['id']}/launch"

    assert ui.post(path, data={"extra_vars": "", "limit": ""}).status_code == 403
    wrong = ui.post(path, data={"extra_vars": "", "limit": "", "csrf_token": "nope"})
    assert wrong.status_code == 403
    cross = ui.post(
        path,
        data={"extra_vars": "", "limit": "", "csrf_token": token},
        headers={"Origin": "https://evil.example"},
    )
    assert cross.status_code == 403
    ok = ui.post(path, data={"extra_vars": "", "limit": "", "csrf_token": token})
    assert ok.status_code == 303
    env.wait(int(ok.headers["location"].rsplit("/", 1)[1]))

    # Een API-call met sessiecookie valt ook onder CSRF.
    api_via_cookie = httpx.post(
        f"{API_URL}/api/v1/projects",
        cookies=ui.cookies,
        json={"name": unique("csrf"), "git_url": "file:///x"},
    )
    assert api_via_cookie.status_code == 403


def test_ui_api_token_shown_once(admin: LocalUser) -> None:
    ui = login_ui(admin.username, admin.password)
    resp = ui.post("/account/tokens", data={"name": "from-ui", "expires_days": "7"})
    assert resp.status_code == 200
    match = re.search(r"(sched_[A-Za-z0-9_-]{20,})", resp.text)
    assert match
    assert httpx.get(f"{API_URL}/api/v1/me", headers=bearer(match.group(1))).status_code == 200
    assert match.group(1) not in ui.get("/account").text


def test_ui_admin_pages(admin: LocalUser, local_users: dict[str, LocalUser]) -> None:
    ui = login_ui(admin.username, admin.password)
    assert "it-viewer" in ui.get("/users").text
    assert "login" in ui.get("/audit").text
    viewer = login_ui(local_users["viewer"].username, local_users["viewer"].password)
    assert viewer.get("/users").status_code == 403
    assert viewer.get("/audit").status_code == 403


def test_ui_admin_cannot_lock_self_out(admin: LocalUser, env: Env) -> None:
    ui = login_ui(admin.username, admin.password)
    admin_id = next(u["id"] for u in env.api.get("/users").json() if u["username"] == "it-admin")
    resp = ui.post(f"/users/{admin_id}", data={"roles": ["viewer"]})
    assert resp.status_code == 400
    assert env.api.get(f"/users/{admin_id}").json()["roles"] == ["admin"]


# --- UI: gebruikersbeheer en wachtwoorden ----------------------------------------------


def test_ui_users_list_layout(admin: LocalUser) -> None:
    ui = login_ui(admin.username, admin.password)
    page = ui.get("/users").text
    assert 'href="/ui/users/new"' in page
    headers = re.findall(r"<th>([^<]*)</th>", page)
    assert headers[-1].startswith("Last login")
    assert "<form" not in page.split("<table", 1)[1].split("</table>", 1)[0]  # geen inline forms


def test_ui_add_local_user(admin: LocalUser) -> None:
    ui = login_ui(admin.username, admin.password)
    assert ui.get("/users/new").status_code == 200
    name = unique("it-ui").lower()
    base = {"username": name, "display_name": "UI Test", "roles": ["operator"]}

    mismatch = ui.post("/users", data={**base, "password": "p" * 12, "password_confirm": "q" * 12})
    assert mismatch.status_code == 400
    assert "do not match" in mismatch.text
    assert name in mismatch.text  # ingevulde waarden blijven staan

    short = ui.post("/users", data={**base, "password": "short", "password_confirm": "short"})
    assert short.status_code == 400
    assert "at least 12" in short.text

    ok = ui.post("/users", data={**base, "password": "p" * 12, "password_confirm": "p" * 12})
    assert ok.status_code == 303
    assert ok.headers["location"] == "/ui/users"
    assert login_ui(name, "p" * 12).get("/runs").status_code == 200


def test_ui_edit_user_and_reset_password(admin: LocalUser, env: Env) -> None:
    target = ensure_local_user(unique("it-edit"), ["viewer"])
    user_id = next(
        u["id"] for u in env.api.get("/users").json() if u["username"] == target.username
    )
    ui = login_ui(admin.username, admin.password)

    edit = ui.get(f"/users/{user_id}/edit")
    assert edit.status_code == 200
    assert f'href="/ui/users/{user_id}/password"' in edit.text
    saved = ui.post(
        f"/users/{user_id}", data={"display_name": "Edited", "roles": ["viewer", "operator"]}
    )
    assert saved.status_code == 303
    after = env.api.get(f"/users/{user_id}").json()
    assert after["roles"] == ["operator", "viewer"]
    assert after["display_name"] == "Edited"

    target_ui = login_ui(target.username, target.password)
    assert ui.get(f"/users/{user_id}/password").status_code == 200
    bad = ui.post(f"/users/{user_id}/password", data={"new": "n" * 12, "confirm": "m" * 12})
    assert bad.status_code == 400
    assert "do not match" in bad.text
    reset = ui.post(f"/users/{user_id}/password", data={"new": "n" * 12, "confirm": "n" * 12})
    assert reset.status_code == 303
    assert target_ui.get("/runs").status_code == 303  # sessie van de gebruiker vervallen
    assert login_ui(target.username, "n" * 12).get("/runs").status_code == 200


def test_ui_change_own_password(env: Env) -> None:
    me = ensure_local_user(unique("it-own"), ["viewer"])
    ui = login_ui(me.username, me.password)
    assert 'href="/ui/account/password"' in ui.get("/account").text
    assert ui.get("/account/password").status_code == 200

    wrong = ui.post(
        "/account/password", data={"current": "nope", "new": "x" * 12, "confirm": "x" * 12}
    )
    assert wrong.status_code == 400
    assert "current password is incorrect" in wrong.text
    differ = ui.post(
        "/account/password", data={"current": me.password, "new": "x" * 12, "confirm": "y" * 12}
    )
    assert differ.status_code == 400
    ok = ui.post(
        "/account/password", data={"current": me.password, "new": "x" * 12, "confirm": "x" * 12}
    )
    assert ok.status_code == 303
    assert ok.headers["location"] == "/ui/login"
    assert login_ui(me.username, "x" * 12).get("/runs").status_code == 200


# --- OIDC: volledige browserflow (authorization code + PKCE) -----------------------------


def _internal(url: str) -> str:
    """Browser-URL's (localhost) omzetten naar de adressen binnen het compose-netwerk."""
    parts = urlsplit(url)
    host = {"localhost:8080": urlsplit(KEYCLOAK).netloc, "localhost:8000": urlsplit(API_URL).netloc}
    return urlunsplit(parts._replace(netloc=host.get(parts.netloc, parts.netloc)))


def oidc_browser_login(user: str) -> httpx.Response:
    c = httpx.Client(follow_redirects=False, timeout=15)
    resp = c.get(f"{API_URL}/ui/auth/oidc/start", params={"next": "/ui/schedules"})
    assert resp.status_code == 303
    login_page = c.get(_internal(resp.headers["location"]))
    assert login_page.status_code == 200
    action = re.search(r'id="kc-form-login"[^>]*action="([^"]+)"', login_page.text)
    assert action, "keycloak login form not found"
    form_url = _internal(action.group(1).replace("&amp;", "&"))
    after = c.post(form_url, data={"username": user, "password": kc_password(user)})
    assert after.status_code == 302, after.text[:500]
    callback = _internal(after.headers["location"])
    result = c.get(callback)
    result.extensions["client"] = c
    return result


def test_oidc_login_flow() -> None:
    resp = oidc_browser_login("kc-operator")
    assert resp.status_code == 303, resp.text[:500]
    assert resp.headers["location"] == "/ui/schedules"
    client: httpx.Client = resp.extensions["client"]
    page = client.get(f"{API_URL}/ui/runs")
    assert page.status_code == 200
    assert "Otto Operator" in page.text
    assert client.get(f"{API_URL}/ui/users").status_code == 403


def test_oidc_login_without_role_is_refused() -> None:
    resp = oidc_browser_login("kc-norole")
    assert resp.status_code == 403
    assert "no role" in resp.text


def test_oidc_callback_rejects_bad_state() -> None:
    c = httpx.Client(follow_redirects=False, timeout=10)
    c.get(f"{API_URL}/ui/auth/oidc/start")
    resp = c.get(f"{API_URL}/ui/auth/callback", params={"code": "x", "state": "forged"})
    assert resp.status_code == 401
    assert "sched_session" not in c.cookies
