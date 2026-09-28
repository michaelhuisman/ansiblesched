"""Phase 5a (unit): proxy headers, version in static URLs, host keys and lock loss."""

import asyncio
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.ui import common
from app.worker.executor import LOCK_LOST_REASON, CancelCheck


def _client_seen_by_app(peer: str, forwarded_for: str, trusted: str) -> str:
    seen: dict[str, Any] = {}

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        seen["client"] = scope["client"]

    scope = {
        "type": "http",
        "scheme": "http",
        "client": (peer, 40000),
        "headers": [(b"x-forwarded-for", forwarded_for.encode())],
    }
    asyncio.run(ProxyHeadersMiddleware(app, trusted_hosts=trusted)(scope, None, None))
    return str(seen["client"][0])


def test_forwarded_for_only_from_trusted_proxy() -> None:
    assert _client_seen_by_app("127.0.0.1", "203.0.113.9", "127.0.0.1") == "203.0.113.9"
    assert _client_seen_by_app("198.51.100.7", "203.0.113.9", "127.0.0.1") == "198.51.100.7"
    # A client forging a chain itself: only the hop before the trusted proxy counts.
    assert _client_seen_by_app("127.0.0.1", "10.0.0.1, 203.0.113.9", "127.0.0.1") == "203.0.113.9"


def test_static_url_versioned_by_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "a.css").write_text("body{}")
    monkeypatch.setattr(common, "STATIC_DIR", tmp_path)
    common._static_version.cache_clear()
    first = common.static("a.css")
    assert first.startswith("/ui/static/a.css?v=")
    (tmp_path / "a.css").write_text("body{color:red}")
    common._static_version.cache_clear()
    assert common.static("a.css") != first
    assert common.static("missing.css").endswith("?v=0")
    assert common.static("../../etc/passwd").endswith("?v=0")


def test_cancel_check_aborts_when_lock_lost() -> None:
    lock_ok = {"value": True}
    check = CancelCheck(None, 1, lambda: lock_ok["value"])  # type: ignore[arg-type]
    check._last_check = float("inf")  # skip the DB check in this test
    check._last_lock_check = 0.0
    assert check() is False
    lock_ok["value"] = False
    check._last_lock_check = 0.0
    assert check() is True
    assert check.lost_lock is True
    assert LOCK_LOST_REASON == "overlap lock lost"


# --- host keys in the executor ------------------------------------------------------


def test_host_key_checking_without_known_hosts_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.config import Settings
    from app.worker.executor import Executor, RunSetupError
    from app.worker.git import RepoCache

    ex = Executor(
        Settings(database_url="postgresql+psycopg://x/y", ansible_host_key_checking=True),
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        RepoCache(tmp_path),
    )
    spec = SimpleNamespace(known_hosts_credential=None)
    with pytest.raises(RunSetupError, match="no known_hosts"):
        ex._host_key_env(spec, tmp_path)  # type: ignore[arg-type]


def test_known_hosts_forces_strict_checking(tmp_path: Path) -> None:
    from app.core.config import Settings
    from app.worker.credentials import CredentialRef
    from app.worker.executor import Executor
    from app.worker.git import RepoCache

    class Resolver:
        def fields(self, ref: CredentialRef) -> dict[str, str]:
            return {"known_hosts": "web1 ssh-ed25519 AAAA"}

    ex = Executor(
        Settings(database_url="postgresql+psycopg://x/y", ansible_host_key_checking=False),
        None,  # type: ignore[arg-type]
        Resolver(),
        RepoCache(tmp_path),
    )
    spec = SimpleNamespace(
        known_hosts_credential=CredentialRef(1, "known_hosts", "ssh/x", "known_hosts")
    )
    env = ex._host_key_env(spec, tmp_path)  # type: ignore[arg-type]
    assert env["ANSIBLE_HOST_KEY_CHECKING"] == "True"
    args = env["ANSIBLE_SSH_COMMON_ARGS"]
    assert f"UserKnownHostsFile={tmp_path / 'known_hosts'}" in args
    assert "StrictHostKeyChecking=yes" in args
    assert "GlobalKnownHostsFile=/dev/null" in args
    assert (tmp_path / "known_hosts").read_text() == "web1 ssh-ed25519 AAAA\n"
    assert stat.S_IMODE((tmp_path / "known_hosts").stat().st_mode) == 0o600
