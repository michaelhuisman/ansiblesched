import httpx

from tests.integration.conftest import Env, post, unique


def test_health(api: httpx.Client) -> None:
    base = str(api.base_url).removesuffix("/api/v1/").removesuffix("/api/v1")
    assert httpx.get(f"{base}/healthz").json() == {"status": "ok"}
    assert httpx.get(f"{base}/readyz").json() == {"status": "ok"}


def test_crud_project(api: httpx.Client) -> None:
    name = unique("crud")
    created = post(api, "/projects", {"name": name, "git_url": "file:///x.git"})
    assert created["branch"] == "main"

    resp = api.put(
        f"/projects/{created['id']}",
        json={"name": name, "git_url": "file:///y.git", "branch": "dev"},
    )
    assert resp.status_code == 200
    assert resp.json()["git_url"] == "file:///y.git"
    assert resp.json()["updated_at"] >= created["updated_at"]

    assert api.get(f"/projects/{created['id']}").json()["branch"] == "dev"
    assert any(p["id"] == created["id"] for p in api.get("/projects").json())

    assert api.delete(f"/projects/{created['id']}").status_code == 204
    assert api.get(f"/projects/{created['id']}").status_code == 404


def test_duplicate_name_conflicts(api: httpx.Client) -> None:
    name = unique("dup")
    post(api, "/projects", {"name": name, "git_url": "file:///x.git"})
    resp = api.post("/projects", json={"name": name, "git_url": "file:///x.git"})
    assert resp.status_code == 409


def test_invalid_reference(api: httpx.Client) -> None:
    resp = api.post(
        "/projects",
        json={"name": unique("ref"), "git_url": "file:///x.git", "credential_id": 999999},
    )
    assert resp.status_code == 422


def test_delete_in_use_conflicts(env: Env) -> None:
    template = env.template("ping.yml")
    assert env.api.delete(f"/projects/{env.project['id']}").status_code == 409
    assert env.api.get(f"/templates/{template['id']}").status_code == 200


def test_credentials_expose_only_references(env: Env) -> None:
    cred = env.api.get(f"/credentials/{env.credential['id']}").json()
    assert set(cred) == {"id", "name", "type", "openbao_path", "openbao_key", "created_at"}


def test_launch_unknown_template(api: httpx.Client) -> None:
    assert api.post("/templates/999999/launch", json={}).status_code == 404


def test_launch_merges_extra_vars_and_limit(env: Env) -> None:
    template = env.template("ping.yml", extra_vars={"a": 1, "b": 2}, limit="ssh-target")
    run = env.launch(template, extra_vars={"b": 3})
    assert run["extra_vars"] == {"a": 1, "b": 3}
    assert run["limit"] == "ssh-target"
    assert run["triggered_by"] == "user:anonymous"
    env.wait(run["id"])


def test_run_filters(env: Env) -> None:
    template = env.template("ping.yml")
    run = env.wait(env.launch(template)["id"])
    runs = env.api.get("/runs", params={"template_id": template["id"]}).json()
    assert [r["id"] for r in runs] == [run["id"]]
    ok = env.api.get("/runs", params={"template_id": template["id"], "status": "failed"}).json()
    assert ok == []
    assert env.api.get("/runs", params={"status": "bogus"}).status_code == 422
