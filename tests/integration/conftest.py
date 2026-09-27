"""Integratietests tegen compose.dev.yml. Draaien in de `dev`-container:

podman compose -f compose.dev.yml run --rm dev pytest tests/integration
"""

import os
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

API_URL = os.environ.get("SCHED_IT_API_URL", "http://127.0.0.1:8000")
FIXTURE_REPO = Path("/fixtures/repo.git")
RUNTIME_DIR = Path("/run/scheduler")
SECRETS_DIR = Path("/secrets")
TERMINAL = {"successful", "failed", "error", "timeout", "canceled", "skipped"}
INLINE_INVENTORY = "ssh-target ansible_user=ansible ansible_python_interpreter=/usr/bin/python3\n"

pytestmark = pytest.mark.integration


@pytest.fixture(scope="session")
def api() -> Iterator[httpx.Client]:
    with httpx.Client(base_url=f"{API_URL}/api/v1", timeout=10) as client:
        try:
            httpx.get(f"{API_URL}/readyz", timeout=5).raise_for_status()
        except httpx.HTTPError as exc:
            pytest.skip(f"API niet bereikbaar op {API_URL}: {exc}")
        yield client


def unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def post(api: httpx.Client, path: str, body: dict[str, Any]) -> dict[str, Any]:
    resp = api.post(path, json=body)
    assert resp.status_code in (200, 201, 202), resp.text
    result: dict[str, Any] = resp.json()
    return result


def fixture_head() -> str:
    return subprocess.run(
        ["git", "-C", str(FIXTURE_REPO), "rev-parse", "refs/heads/main"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


class Env:
    """Basisconfiguratie: credential, project en inline inventory tegen ssh-target."""

    def __init__(self, api: httpx.Client) -> None:
        self.api = api
        self.credential = post(
            api,
            "/credentials",
            {
                "name": unique("key"),
                "type": "ssh_key",
                "openbao_path": "ssh-target",
                "openbao_key": "id_ed25519",
            },
        )
        self.project = post(
            api,
            "/projects",
            {"name": unique("proj"), "git_url": f"file://{FIXTURE_REPO}", "branch": "main"},
        )
        self.inventory = post(
            api,
            "/inventories",
            {"name": unique("inv"), "source_type": "inline", "content": INLINE_INVENTORY},
        )

    def template(self, playbook: str, **overrides: Any) -> dict[str, Any]:
        body: dict[str, Any] = {
            "name": unique(playbook.removesuffix(".yml")),
            "project_id": self.project["id"],
            "playbook_path": playbook,
            "inventory_id": self.inventory["id"],
            "machine_credential_id": self.credential["id"],
        }
        body.update(overrides)
        return post(self.api, "/templates", body)

    def launch(self, template: dict[str, Any], **body: Any) -> dict[str, Any]:
        return post(self.api, f"/templates/{template['id']}/launch", body)

    def get_run(self, run_id: int) -> dict[str, Any]:
        resp = self.api.get(f"/runs/{run_id}")
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
        return result

    def wait(
        self, run_id: int, statuses: set[str] = TERMINAL, timeout: float = 90
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            run = self.get_run(run_id)
            if run["status"] in statuses:
                return run
            if time.monotonic() > deadline:
                pytest.fail(f"run {run_id} stuck in {run['status']}")
            time.sleep(0.5)

    def events(self, run_id: int) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        after = 0
        while True:
            resp = self.api.get(f"/runs/{run_id}/events", params={"after_seq": after})
            resp.raise_for_status()
            page = resp.json()
            items.extend(page["items"])
            if page["next_after_seq"] is None:
                return items
            after = page["next_after_seq"]


@pytest.fixture(scope="session")
def env(api: httpx.Client) -> Env:
    return Env(api)
