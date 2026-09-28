import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import stream
from app.services.stream import Clock, parse_last_event_id, run_stream, sse


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class FakeDb:
    """Script of (status, events) per poll round."""

    def __init__(self, rounds: list[tuple[str, list[int]]]) -> None:
        self.rounds = rounds
        self.i = -1
        self.after_seqs: list[int] = []

    @contextmanager
    def session(self) -> Iterator[None]:
        self.i = min(self.i + 1, len(self.rounds) - 1)
        yield None

    def get(self, _session: Any, run_id: int) -> SimpleNamespace:
        status, _ = self.rounds[self.i]
        return SimpleNamespace(
            id=run_id, status=status, rc=None, status_reason=None, started_at=None, finished_at=None
        )

    def list_events(
        self, _session: Any, _run_id: int, *, after_seq: int, limit: int
    ) -> list[SimpleNamespace]:
        self.after_seqs.append(after_seq)
        _, seqs = self.rounds[self.i]
        return [
            SimpleNamespace(seq=s, event="runner_on_ok", host="h", task="t", stdout=f"line {s}")
            for s in seqs
            if s > after_seq
        ][:limit]


def parse(messages: list[str]) -> list[tuple[str, dict[str, Any] | None, str | None]]:
    out: list[tuple[str, dict[str, Any] | None, str | None]] = []
    for msg in messages:
        if msg.startswith((":", "retry:")):
            out.append(("comment", None, None))
            continue
        fields = dict(line.split(": ", 1) for line in msg.strip().splitlines())
        out.append((fields["event"], json.loads(fields["data"]), fields.get("id")))
    return out


Install = Callable[[list[tuple[str, list[int]]]], FakeDb]


@pytest.fixture
def db(monkeypatch: pytest.MonkeyPatch) -> Install:
    def install(rounds: list[tuple[str, list[int]]]) -> FakeDb:
        fake = FakeDb(rounds)
        monkeypatch.setattr(stream.runs, "get", fake.get)
        monkeypatch.setattr(stream.runs, "list_events", fake.list_events)
        return fake

    return install


def _run(fake: FakeDb, after_seq: int = 0) -> list[str]:
    clock = FakeClock()
    return list(
        run_stream(fake.session, 7, after_seq=after_seq, clock=Clock(clock.now, clock.sleep))  # type: ignore[arg-type]
    )


def test_streams_events_status_and_end(db: Install) -> None:
    fake = db([("running", [1, 2]), ("running", [1, 2, 3]), ("successful", [1, 2, 3, 4])])
    msgs = parse(_run(fake))
    kinds = [k for k, _, _ in msgs if k != "comment"]
    assert kinds == [
        "run_event",
        "run_event",
        "status",  # ronde 1
        "run_event",  # ronde 2
        "run_event",
        "status",
        "end",  # ronde 3
    ]
    ids = [i for k, _, i in msgs if k == "run_event"]
    assert ids == ["1", "2", "3", "4"]
    assert fake.after_seqs == [0, 2, 3]
    statuses = [d["status"] for k, d, _ in msgs if k == "status" and d]
    assert statuses == ["running", "successful"]


def test_resume_after_last_event_id(db: Install) -> None:
    fake = db([("successful", [1, 2, 3, 4])])
    ids = [i for k, _, i in parse(_run(fake, after_seq=2)) if k == "run_event"]
    assert ids == ["3", "4"]


def test_finished_run_ends_immediately(db: Install) -> None:
    fake = db([("failed", [])])
    kinds = [k for k, _, _ in parse(_run(fake)) if k != "comment"]
    assert kinds == ["status", "end"]


def test_keepalive_when_idle(db: Install, monkeypatch: pytest.MonkeyPatch) -> None:
    rounds: list[tuple[str, list[int]]] = [("running", [])] * 40 + [("successful", [])]
    fake = db(rounds)
    monkeypatch.setattr(stream, "KEEPALIVE_S", 5.0)
    msgs = _run(fake)
    assert any(m.startswith(": keepalive") for m in msgs)


def test_sse_format_is_single_data_line() -> None:
    msg = sse("run_event", {"stdout": "a\nb"}, event_id=3)
    assert msg.endswith("\n\n")
    lines = msg.strip().split("\n")
    assert lines == ["event: run_event", "id: 3", 'data: {"stdout": "a\\nb"}']


@pytest.mark.parametrize(
    ("raw", "expected"), [(None, 0), ("", 0), ("12", 12), ("abc", 0), ("-3", 0)]
)
def test_parse_last_event_id(raw: str | None, expected: int) -> None:
    assert parse_last_event_id(raw) == expected
