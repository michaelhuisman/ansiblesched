import json

from tests.integration.conftest import RUNTIME_DIR, SECRETS_DIR, Env, fixture_head, post, unique


def _assert_cleaned_up(run_id: int) -> None:
    assert not (RUNTIME_DIR / str(run_id)).exists(), "private data dir not removed"


def test_ping_successful(env: Env) -> None:
    run = env.wait(env.launch(env.template("ping.yml"))["id"])

    assert run["status"] == "successful", run
    assert run["rc"] == 0
    assert run["commit_sha"] == fixture_head()
    assert run["worker_id"]
    assert run["started_at"]
    assert run["finished_at"]
    assert run["stats"]["ok"] == {"ssh-target": 1}
    assert run["stats"]["failed"] == {}

    events = env.events(run["id"])
    kinds = [e["event"] for e in events]
    assert "runner_on_ok" in kinds
    assert "playbook_on_stats" in kinds
    assert [e["seq"] for e in events] == sorted({e["seq"] for e in events})
    _assert_cleaned_up(run["id"])


def test_project_file_inventory_and_changes(env: Env) -> None:
    inventory = post(
        env.api,
        "/inventories",
        {
            "name": unique("inv-file"),
            "source_type": "project_file",
            "project_id": env.project["id"],
            "path": "inventory/hosts.ini",
        },
    )
    template = env.template("file.yml", inventory_id=inventory["id"])
    marker = unique("marker")
    run = env.wait(env.launch(template, extra_vars={"marker": marker})["id"])

    assert run["status"] == "successful", run
    assert run["extra_vars"] == {"marker": marker}
    assert run["stats"]["changed"] == {"ssh-target": 1}
    _assert_cleaned_up(run["id"])


def test_failing_playbook(env: Env) -> None:
    run = env.wait(env.launch(env.template("fail.yml"))["id"])

    assert run["status"] == "failed", run
    assert run["rc"] not in (None, 0)
    assert run["stats"]["failed"] == {"ssh-target": 1}
    _assert_cleaned_up(run["id"])


def test_timeout(env: Env) -> None:
    template = env.template("sleep.yml", timeout_s=3, extra_vars={"sleep_s": 60})
    run = env.wait(env.launch(template)["id"], timeout=60)

    assert run["status"] == "timeout", run
    assert "timeout" in (run["status_reason"] or "")
    _assert_cleaned_up(run["id"])


def test_cancel_running(env: Env) -> None:
    template = env.template("sleep.yml", extra_vars={"sleep_s": 60})
    run_id = env.launch(template)["id"]
    env.wait(run_id, {"running"})
    # Wait until ansible is really running (first task started).
    for _ in range(40):
        if any(e["event"] == "playbook_on_task_start" for e in env.events(run_id)):
            break
        env.wait(run_id, {"running"}, timeout=1)

    resp = env.api.post(f"/runs/{run_id}/cancel")
    assert resp.status_code == 202, resp.text
    run = env.wait(run_id, timeout=30)

    assert run["status"] == "canceled", run
    assert run["cancel_requested_at"]
    _assert_cleaned_up(run_id)

    # A finished run cannot be canceled again.
    assert env.api.post(f"/runs/{run_id}/cancel").status_code == 409


def test_setup_error_cleans_up(env: Env) -> None:
    broken = post(
        env.api,
        "/credentials",
        {
            "name": unique("missing"),
            "type": "ssh_key",
            "openbao_path": "ssh/does-not-exist",
            "openbao_key": "id_ed25519",
        },
    )
    run = env.wait(env.launch(env.template("ping.yml", machine_credential_id=broken["id"]))["id"])

    assert run["status"] == "error", run
    assert f"credential {broken['id']}" in run["status_reason"]
    # The checkout already happened; so the dir existed and must be gone.
    assert run["commit_sha"] == fixture_head()
    _assert_cleaned_up(run["id"])


def test_secrets_never_in_events(env: Env) -> None:
    secret = unique("it-secret-value")
    run = env.wait(env.launch(env.template("nolog.yml"), extra_vars={"secret_value": secret})["id"])
    assert run["status"] == "successful", run

    events = env.events(run["id"])
    dumped = json.dumps(events)
    assert secret not in dumped

    ssh_key = (SECRETS_DIR / "ssh-target" / "id_ed25519").read_text()
    for line in ssh_key.splitlines():
        if len(line.strip()) >= 6 and "-----" not in line:
            assert line.strip() not in dumped
    # The no_log task is visible, but without content.
    assert any("censored" in json.dumps(e["data"]) for e in events)
