import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from app.core.auth import Action, Principal
from app.services.notifications import (
    SIGNATURE_HEADER,
    WebhookConfig,
    _send,
    backoff,
    build_payload,
    fingerprint,
    sign,
)

URL = "https://hooks.example.invalid/services/T000/B000/secret-token-123"


def _run() -> SimpleNamespace:
    return SimpleNamespace(
        id=42,
        schedule_id=7,
        status="failed",
        rc=2,
        status_reason=None,
        triggered_by="schedule",
        scheduled_for=datetime(2026, 9, 27, 12, 0, tzinfo=UTC),
        started_at=datetime(2026, 9, 27, 12, 0, 1, tzinfo=UTC),
        finished_at=datetime(2026, 9, 27, 12, 0, 9, tzinfo=UTC),
    )


def _template() -> SimpleNamespace:
    return SimpleNamespace(id=3, name="backup")


def test_fingerprint_is_stable_and_hides_url() -> None:
    fp = fingerprint(URL)
    assert fp == fingerprint(URL)
    assert len(fp) == 16
    assert "secret" not in fp
    assert fingerprint(URL + "x") != fp


def test_webhook_config_from_secret() -> None:
    config = WebhookConfig.from_secret({"urls": f'["{URL}"]', "hmac_secret": "h"})
    assert config.targets() == {fingerprint(URL): URL}
    assert config.hmac_secret == "h"
    assert "secret-token" not in repr(config)
    assert "h'" not in repr(config)
    assert WebhookConfig.from_secret({}).urls == ()


@pytest.mark.parametrize("raw", ["not json", '{"a": 1}', "[1, 2]"])
def test_webhook_config_rejects_bad_urls(raw: str) -> None:
    with pytest.raises(ValueError, match="JSON list"):
        WebhookConfig.from_secret({"urls": raw})


def test_backoff_grows_and_caps() -> None:
    assert [backoff(n).total_seconds() for n in (1, 2, 3, 4)] == [5, 10, 20, 40]
    assert backoff(20) == timedelta(minutes=10)


def test_signature_matches_hmac_sha256() -> None:
    body = b'{"a": 1}'
    expected = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert sign(body, "s3cret") == f"sha256={expected}"


def test_payload_contents() -> None:
    payload = build_payload(_run(), _template(), "run.failed", "https://sched.example/")  # type: ignore[arg-type]
    assert payload["run_id"] == 42
    assert payload["template"] == {"id": 3, "name": "backup"}
    assert payload["status"] == "failed"
    assert payload["url"] == "https://sched.example/ui/runs/42"
    assert payload["scheduled_for"] == "2026-09-27T12:00:00+00:00"


def _client(handler: httpx.MockTransport) -> httpx.Client:
    return httpx.Client(transport=handler)


def _note() -> SimpleNamespace:
    return SimpleNamespace(id=9, event="run.failed")


def test_send_success_with_signature() -> None:
    seen: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["req"] = request
        return httpx.Response(204)

    err = _send(
        _client(httpx.MockTransport(handler)),
        URL,
        _note(),
        _run(),
        _template(),
        "http://x",
        "s3cret",
    )  # type: ignore[arg-type]
    assert err is None
    req = seen["req"]
    assert req.headers[SIGNATURE_HEADER] == sign(req.content, "s3cret")
    assert req.headers["X-Lamplighter-Event"] == "run.failed"
    assert json.loads(req.content)["run_id"] == 42


def test_send_without_secret_has_no_signature() -> None:
    seen: dict[str, httpx.Request] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["req"] = request
        return httpx.Response(200)

    _send(
        _client(httpx.MockTransport(handler)), URL, _note(), _run(), _template(), "http://x", None
    )  # type: ignore[arg-type]
    assert SIGNATURE_HEADER not in seen["req"].headers


def test_send_http_error_message_has_no_url() -> None:
    client = _client(httpx.MockTransport(lambda _r: httpx.Response(503)))
    err = _send(client, URL, _note(), _run(), _template(), "http://x", None)  # type: ignore[arg-type]
    assert err == "http 503"


def test_send_connection_error_message_has_no_url() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot connect to {request.url}")

    err = _send(
        _client(httpx.MockTransport(boom)), URL, _note(), _run(), _template(), "http://x", None
    )  # type: ignore[arg-type]
    assert err == "request failed: ConnectError"
    assert "secret-token" not in (err or "")


def test_send_unknown_target() -> None:
    client = _client(httpx.MockTransport(lambda _r: pytest.fail("must not send")))
    assert (
        _send(client, None, _note(), _run(), _template(), "http://x", None)
        == "target no longer configured"
    )  # type: ignore[arg-type]


# --- auth-naad -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("roles", "allowed"),
    [
        ({"viewer"}, {Action.READ}),
        ({"operator"}, {Action.READ, Action.LAUNCH, Action.CANCEL}),
        ({"admin"}, set(Action)),
        ({"operator", "admin"}, set(Action)),
        (set(), set()),
        ({"viewer", "operator"}, {Action.READ, Action.LAUNCH, Action.CANCEL}),
    ],
)
def test_principal_permissions(roles: set[str], allowed: set[Action]) -> None:
    user = Principal("u", frozenset(roles))
    assert {a for a in Action if user.can(a)} == allowed


def test_principal_identity() -> None:
    local = Principal("local:alice", frozenset({"viewer"}))
    assert local.triggered_by == "user:local:alice"
    assert local.label == "alice"
    assert not local.can(Action.MANAGE_USERS)
    assert Principal("oidc:abc", display_name="Ada").label == "Ada"
