import json
from datetime import UTC, datetime

from app.services.events import REDACTED, SecretMasker, filter_event

SSH_KEY = (
    "-----BEGIN OPENSSH PRIVATE KEY-----\n"
    "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW\n"
    "QyNTUxOQAAACBkZXYta2V5LWZvci10ZXN0cy1vbmx5LW5vdC1yZWFsLWF0LWFsbA==\n"
    "-----END OPENSSH PRIVATE KEY-----\n"
)


def _event(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "counter": 7,
        "event": "runner_on_ok",
        "stdout": "ok: [web1]",
        "created": "2026-09-27T12:00:00.000000+00:00",
        "event_data": {"host": "web1", "task": "Ping", "res": {"ping": "pong", "changed": False}},
    }
    base.update(overrides)
    return base


def test_basic_fields() -> None:
    ev = filter_event(_event(), SecretMasker([]))
    assert ev is not None
    assert ev.seq == 7
    assert ev.event == "runner_on_ok"
    assert ev.host == "web1"
    assert ev.task == "Ping"
    assert ev.stdout == "ok: [web1]"
    assert ev.created_at == datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
    assert ev.data["res"] == {"ping": "pong", "changed": False}


def test_event_without_counter_is_dropped() -> None:
    assert filter_event(_event(counter=None), SecretMasker([])) is None


def test_no_log_result_is_censored_and_stdout_dropped() -> None:
    raw = _event(
        stdout='ok: [web1] => {"msg": "the secret is hunter2hunter2"}',
        event_data={
            "host": "web1",
            "res": {"msg": "the secret is hunter2hunter2", "_ansible_no_log": True},
        },
    )
    ev = filter_event(raw, SecretMasker([]))
    assert ev is not None
    assert ev.stdout == ""
    assert "hunter2" not in json.dumps(ev.data)
    assert "censored" in ev.data["res"]


def test_no_log_inside_loop_results() -> None:
    raw = _event(
        event_data={
            "host": "web1",
            "res": {"results": [{"item": "a"}, {"item": "topsecret", "_ansible_no_log": True}]},
        }
    )
    ev = filter_event(raw, SecretMasker([]))
    assert ev is not None
    assert "topsecret" not in json.dumps(ev.data)
    assert ev.stdout == ""


def test_censored_marker_counts_as_no_log() -> None:
    raw = _event(event_data={"res": {"censored": "hidden"}})
    ev = filter_event(raw, SecretMasker([]))
    assert ev is not None
    assert ev.stdout == ""


def test_secret_keys_are_redacted_recursively() -> None:
    raw = _event(
        event_data={
            "res": {
                "ansible_facts": {"db_password": "pw-value-1", "db_user": "app"},
                "invocation": {"module_args": {"api_token": "tok-123456", "url": "https://x"}},
            },
            "ansible_ssh_pass": "sshpass-value",
            "ansible_become_pass": "become-value",
            "private_key": "---",
        }
    )
    ev = filter_event(raw, SecretMasker([]))
    assert ev is not None
    dumped = json.dumps(ev.data)
    for value in ("pw-value-1", "tok-123456", "sshpass-value", "become-value"):
        assert value not in dumped
    assert ev.data["res"]["ansible_facts"]["db_user"] == "app"
    assert ev.data["res"]["ansible_facts"]["db_password"] == REDACTED


def test_innocent_keys_are_kept() -> None:
    raw = _event(event_data={"res": {"passed": True, "bypass_cache": "yes", "compass": "north"}})
    ev = filter_event(raw, SecretMasker([]))
    assert ev is not None
    assert ev.data["res"] == {"passed": True, "bypass_cache": "yes", "compass": "north"}


def test_known_secret_values_are_masked_everywhere() -> None:
    masker = SecretMasker([SSH_KEY, "vault-pass-xyz"])
    key_line = SSH_KEY.splitlines()[1]
    raw = _event(
        stdout=f"debug: {key_line} and vault-pass-xyz",
        event_data={"res": {"msg": ["nested", f"x{key_line}y"], "out": SSH_KEY}},
    )
    ev = filter_event(raw, masker)
    assert ev is not None
    dumped = json.dumps(ev.data) + ev.stdout
    assert key_line not in dumped
    assert "vault-pass-xyz" not in dumped
    assert REDACTED in ev.stdout


def test_short_secrets_are_not_masked() -> None:
    masker = SecretMasker(["abc"])
    assert masker.mask("abcdef") == "abcdef"


def test_nul_bytes_are_stripped() -> None:
    raw = _event(stdout="a\x00b", event_data={"res": {"out": "c\x00d"}})
    ev = filter_event(raw, SecretMasker([]))
    assert ev is not None
    assert ev.stdout == "ab"
    assert ev.data["res"]["out"] == "cd"


def test_naive_created_is_assumed_utc() -> None:
    ev = filter_event(_event(created="2026-01-01T00:00:00"), SecretMasker([]))
    assert ev is not None
    assert ev.created_at.tzinfo is UTC


def test_as_row_contains_run_id() -> None:
    ev = filter_event(_event(), SecretMasker([]))
    assert ev is not None
    row = ev.as_row(42)
    assert row["run_id"] == 42
    assert row["seq"] == 7
