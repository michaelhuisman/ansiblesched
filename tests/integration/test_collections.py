"""Ansible collections: the standard set in the image and a project's own
collections/requirements.yml (installed with ansible-galaxy, cached per requirements)."""

from pathlib import Path
from typing import Any

from tests.integration.conftest import Env, post, unique

FIXTURES = Path("/fixtures")
COLLECTIONS_CACHE = Path("/var/cache/lamplighter/collections")


def _project_template(env: Env, repo: str, playbook: str) -> dict[str, Any]:
    project = post(
        env.api,
        "/projects",
        {"name": unique("coll"), "git_url": f"file://{FIXTURES / repo}", "branch": "main"},
    )
    return env.template(playbook, project_id=project["id"])


def _note(env: Env, run_id: int) -> str | None:
    notes = [e for e in env.events(run_id) if e["event"] == "lamplighter_note"]
    return notes[0]["stdout"] if notes else None


def test_standard_collections_from_the_image(env: Env) -> None:
    run = env.wait(env.launch(env.template("standard-collections.yml"))["id"])
    assert run["status"] == "successful", run
    # No requirements.yml in this repo: no note, no project collections.
    assert _note(env, run["id"]) is None


def test_project_collections_are_installed_and_cached(env: Env) -> None:
    template = _project_template(env, "collections.git", "hello.yml")
    first = env.wait(env.launch(template)["id"], timeout=180)
    assert first["status"] == "successful", first
    note = _note(env, first["id"])
    assert note is not None
    assert note.startswith("Collections from collections/requirements.yml:")

    second = env.wait(env.launch(template)["id"], timeout=120)
    assert second["status"] == "successful", second
    assert _note(env, second["id"]) == "Collections from collections/requirements.yml: cached"

    installed = list(COLLECTIONS_CACHE.glob("*/ansible_collections/lamplighter_test/fixtures"))
    assert installed, "collection not in the shared cache"
    assert not list(COLLECTIONS_CACHE.glob(".tmp-*")), "temp install dir left behind"


def test_broken_requirements_end_the_run_with_error(env: Env) -> None:
    template = _project_template(env, "collections-bad.git", "hello.yml")
    run = env.wait(env.launch(template)["id"], timeout=180)
    assert run["status"] == "error", run
    assert run["status_reason"].startswith("collection install failed")
    assert "missing-9.9.9.tar.gz" in run["status_reason"]
