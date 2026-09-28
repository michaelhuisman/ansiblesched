"""UI-beheer van projecten, inventories en credentials."""

import os
import re
from collections.abc import Iterator

import httpx
import pytest

from tests.integration.conftest import (
    Env,
    LocalUser,
    ensure_local_user,
    fixture_head,
    login_ui,
    unique,
)

GIT_HTTP = os.environ.get("LAMPLIGHTER_IT_GIT_HTTP", "http://127.0.0.1:8081")


@pytest.fixture
def ui(admin: LocalUser) -> Iterator[httpx.Client]:
    client = login_ui(admin.username, admin.password)
    yield client
    client.close()


def find_id(api: httpx.Client, kind: str, name: str) -> int:
    return next(o["id"] for o in api.get(f"/{kind}").json() if o["name"] == name)


def test_pages_render(ui: httpx.Client) -> None:
    for kind in ("projects", "inventories", "credentials"):
        assert ui.get(f"/{kind}").status_code == 200, kind
        assert ui.get(f"/{kind}/new").status_code == 200, kind
    nav = ui.get("/runs").text
    for link in ("/ui/projects", "/ui/inventories", "/ui/credentials"):
        assert f'href="{link}"' in nav


def test_credential_form_never_asks_for_secret(ui: httpx.Client, env: Env) -> None:
    page = ui.get("/credentials/new").text
    assert 'type="password"' not in page
    main = page.split("<main>", 1)[1]
    inputs = set(re.findall(r'<(?:input|select|textarea)[^>]*name="([a-z_]+)"', main))
    assert inputs <= {"csrf_token", "name", "type", "openbao_path", "openbao_key"}

    name = unique("ui-cred")
    missing = ui.post("/credentials", data={"name": name, "type": "ssh_key", "openbao_key": "k"})
    assert missing.status_code == 422
    ok = ui.post(
        "/credentials",
        data={
            "name": name,
            "type": "ssh_key",
            "openbao_path": "ssh/x",
            "openbao_key": "id_ed25519",
        },
    )
    assert ok.status_code == 303
    assert "secret/ssh/x" in ui.get("/credentials").text

    dup = ui.post(
        "/credentials",
        data={"name": name, "type": "ssh_key", "openbao_path": "ssh/y", "openbao_key": "k"},
    )
    assert dup.status_code == 422
    assert "already exists" in dup.text

    cred_id = find_id(env.api, "credentials", name)
    edited = ui.post(
        f"/credentials/{cred_id}",
        data={"name": name, "type": "ssh_key", "openbao_path": "ssh/z", "openbao_key": "id_rsa"},
    )
    assert edited.status_code == 303
    assert env.api.get(f"/credentials/{cred_id}").json()["openbao_key"] == "id_rsa"


def test_project_form_only_offers_git_credentials(ui: httpx.Client, env: Env) -> None:
    git_name, ssh_name = unique("ui-git"), unique("ui-ssh")
    for name, type_ in ((git_name, "git_token"), (ssh_name, "ssh_key")):
        ui.post(
            "/credentials",
            data={"name": name, "type": type_, "openbao_path": f"x/{name}", "openbao_key": "k"},
        )
    page = ui.get("/projects/new").text
    assert git_name in page
    assert ssh_name not in page

    name = unique("ui-proj")
    bad = ui.post("/projects", data={"name": name, "git_url": "", "branch": "main"})
    assert bad.status_code == 422
    ok = ui.post(
        "/projects",
        data={
            "name": name,
            "git_url": "https://git.example/x.git",
            "branch": "main",
            "credential_id": str(find_id(env.api, "credentials", git_name)),
        },
    )
    assert ok.status_code == 303
    project = env.api.get(f"/projects/{find_id(env.api, 'projects', name)}").json()
    assert project["credential_id"] == find_id(env.api, "credentials", git_name)


def test_inventory_sources(ui: httpx.Client, env: Env) -> None:
    project_id = env.project["id"]
    # project_file zonder project: validatiefout
    bad = ui.post(
        "/inventories",
        data={"name": unique("inv"), "source_type": "project_file", "path": "inventory/hosts.ini"},
    )
    assert bad.status_code == 422
    assert "project_id" in bad.text

    file_name = unique("inv-file")
    ok = ui.post(
        "/inventories",
        data={
            "name": file_name,
            "source_type": "project_file",
            "project_id": str(project_id),
            "path": "inventory/hosts.ini",
            "content": "ignored when source is a file",
        },
    )
    assert ok.status_code == 303
    inv = env.api.get(f"/inventories/{find_id(env.api, 'inventories', file_name)}").json()
    assert inv["path"] == "inventory/hosts.ini"
    assert inv["content"] is None

    inline_name = unique("inv-inline")
    ok = ui.post(
        "/inventories",
        data={
            "name": inline_name,
            "source_type": "inline",
            "path": "ignored/when/inline",
            "content": "[web]\nweb1 ansible_user=deploy",
        },
    )
    assert ok.status_code == 303
    inv = env.api.get(f"/inventories/{find_id(env.api, 'inventories', inline_name)}").json()
    assert inv["path"] is None
    assert inv["content"].startswith("[web]")
    assert "2 line(s)" in ui.get("/inventories").text


def test_delete_in_use_and_unused(ui: httpx.Client, env: Env) -> None:
    env.template("ping.yml")  # zorgt dat de credential zeker in gebruik is
    in_use = ui.post(f"/credentials/{env.credential['id']}/delete")
    assert in_use.status_code == 409
    assert "in use" in in_use.text

    name = unique("ui-del")
    ui.post(
        "/credentials",
        data={"name": name, "type": "ssh_key", "openbao_path": "ssh/d", "openbao_key": "k"},
    )
    cred_id = find_id(env.api, "credentials", name)
    gone = ui.post(f"/credentials/{cred_id}/delete")
    assert gone.status_code == 200
    assert gone.headers["HX-Refresh"] == "true"
    assert env.api.get(f"/credentials/{cred_id}").status_code == 404
    audit = env.api.get("/audit", params={"action": "credentials.delete", "limit": 5}).json()
    assert any(e["object_id"] == str(cred_id) for e in audit)


def test_viewer_is_read_only() -> None:
    viewer = ensure_local_user("it-viewer", ["viewer"])
    ui = login_ui(viewer.username, viewer.password)
    for kind in ("projects", "inventories", "credentials"):
        page = ui.get(f"/{kind}")
        assert page.status_code == 200
        assert f'href="/ui/{kind}/new"' not in page.text
        assert ui.get(f"/{kind}/new").status_code == 403
        assert ui.post(f"/{kind}", data={"name": "x"}).status_code == 403


def test_end_to_end_run_from_ui_config(ui: httpx.Client, env: Env) -> None:
    """Alles via de nieuwe pagina's: git-token-credential, project via https en een
    inventory uit de repo. De run moet slagen."""
    cred, project, inventory = unique("e2e-git"), unique("e2e-proj"), unique("e2e-inv")
    ui.post(
        "/credentials",
        data={
            "name": cred,
            "type": "git_token",
            "openbao_path": "git/fixtures",
            "openbao_key": "token",
        },
    )
    ui.post(
        "/projects",
        data={
            "name": project,
            "git_url": f"{GIT_HTTP}/repo.git",
            "branch": "main",
            "credential_id": str(find_id(env.api, "credentials", cred)),
        },
    )
    project_id = find_id(env.api, "projects", project)
    ui.post(
        "/inventories",
        data={
            "name": inventory,
            "source_type": "project_file",
            "project_id": str(project_id),
            "path": "inventory/hosts.ini",
        },
    )
    template = env.api.post(
        "/templates",
        json={
            "name": unique("e2e-tpl"),
            "project_id": project_id,
            "playbook_path": "ping.yml",
            "inventory_id": find_id(env.api, "inventories", inventory),
            "machine_credential_id": env.credential["id"],
        },
    ).json()
    run = env.wait(env.launch(template)["id"])
    assert run["status"] == "successful", run
    assert run["commit_sha"] == fixture_head()
