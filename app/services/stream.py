"""Server-Sent Events voor een run: events en statuswijzigingen, tot de run klaar is."""

import json
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.models.run import FINAL_STATUSES
from app.services import runs

POLL_INTERVAL_S = 0.5
KEEPALIVE_S = 15.0
# Daarna sluit de server de stream; de browser verbindt opnieuw met Last-Event-ID.
MAX_STREAM_S = 3600.0
BATCH = 500


def sse(event: str, data: dict[str, Any], event_id: int | None = None) -> str:
    lines = [f"event: {event}"]
    if event_id is not None:
        lines.append(f"id: {event_id}")
    # json.dumps geeft één regel; SSE-data mag geen kale newlines bevatten.
    lines.append(f"data: {json.dumps(data, default=str)}")
    return "\n".join(lines) + "\n\n"


@dataclass
class Clock:
    now: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


def run_stream(
    sm: sessionmaker[Session], run_id: int, after_seq: int = 0, clock: Clock | None = None
) -> Iterator[str]:
    """Yield SSE-berichten voor `run_id` vanaf `after_seq`.

    Volgorde per ronde: eerst de status, dan de events. Een run is pas echt klaar als
    de status al final was vóórdat we een lege batch lazen; de worker schrijft alle
    events weg voordat hij de status afrondt.
    """
    clock = clock or Clock()
    started = last_output = clock.now()
    last_status: str | None = None
    yield "retry: 2000\n\n"
    while True:
        with sm() as session:
            run = runs.get(session, run_id)
            status = run.status
            events = runs.list_events(session, run_id, after_seq=after_seq, limit=BATCH)
            status_payload = {
                "status": status,
                "rc": run.rc,
                "status_reason": run.status_reason,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
            }

        for event in events:
            yield sse(
                "run_event",
                {
                    "seq": event.seq,
                    "event": event.event,
                    "host": event.host,
                    "task": event.task,
                    "stdout": event.stdout or "",
                },
                event_id=event.seq,
            )
            after_seq = event.seq
        if status != last_status:
            yield sse("status", status_payload)
            last_status = status
            last_output = clock.now()
        elif events:
            last_output = clock.now()

        if status in FINAL_STATUSES and len(events) < BATCH:
            yield sse("end", {"status": status})
            return
        if clock.now() - started > MAX_STREAM_S:
            return
        if clock.now() - last_output > KEEPALIVE_S:
            yield ": keepalive\n\n"
            last_output = clock.now()
        if len(events) < BATCH:
            clock.sleep(POLL_INTERVAL_S)


def parse_last_event_id(value: str | None) -> int:
    if value and value.isdigit():
        return int(value)
    return 0
