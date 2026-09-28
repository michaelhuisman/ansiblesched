import pickle
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.api.schemas import ScheduleIn
from app.scheduler.trigger import (
    DstSafeCronTrigger,
    InvalidScheduleError,
    build_trigger,
    latest_fire_time,
    normalize_day_of_week,
)

AMS = ZoneInfo("Europe/Amsterdam")


def fires_between(trigger: DstSafeCronTrigger, start: datetime, end: datetime) -> list[datetime]:
    out: list[datetime] = []
    nxt = trigger.get_next_fire_time(None, start)
    while nxt is not None and nxt < end:
        out.append(nxt)
        nxt = trigger.get_next_fire_time(nxt, nxt)
    return out


def local_day(year: int, month: int, day: int) -> tuple[datetime, datetime]:
    start = datetime(year, month, day, tzinfo=AMS)
    return start, datetime(year, month, day + 1, tzinfo=AMS)


# --- DST: herfst (2026-10-25, 03:00 CEST -> 02:00 CET) ----------------------


def test_fall_back_fixed_hour_fires_once() -> None:
    fires = fires_between(build_trigger("30 2 * * *", "Europe/Amsterdam"), *local_day(2026, 10, 25))
    assert [f.astimezone(UTC) for f in fires] == [datetime(2026, 10, 25, 0, 30, tzinfo=UTC)]


def test_fall_back_fixed_hour_next_days_unaffected() -> None:
    trigger = build_trigger("30 2 * * *", "Europe/Amsterdam")
    fires = fires_between(
        trigger, datetime(2026, 10, 24, tzinfo=AMS), datetime(2026, 10, 27, tzinfo=AMS)
    )
    assert [f.astimezone(UTC) for f in fires] == [
        datetime(2026, 10, 24, 0, 30, tzinfo=UTC),  # CEST
        datetime(2026, 10, 25, 0, 30, tzinfo=UTC),  # CEST, eerste 02:30
        datetime(2026, 10, 26, 1, 30, tzinfo=UTC),  # CET
    ]


def test_fall_back_hourly_fires_every_real_hour() -> None:
    fires = fires_between(build_trigger("0 * * * *", "Europe/Amsterdam"), *local_day(2026, 10, 25))
    assert len(fires) == 25
    utc = [f.astimezone(UTC) for f in fires]
    assert all(b - a == timedelta(hours=1) for a, b in pairwise(utc))


def test_fall_back_hour_step_counts_as_interval() -> None:
    fires = fires_between(
        build_trigger("0 */2 * * *", "Europe/Amsterdam"), *local_day(2026, 10, 25)
    )
    two_am = [f for f in fires if f.hour == 2]
    assert len(two_am) == 2


# --- DST: voorjaar (2026-03-29, 02:00 CET -> 03:00 CEST) --------------------


def test_spring_forward_fixed_hour_fires_once_after_jump() -> None:
    fires = fires_between(build_trigger("30 2 * * *", "Europe/Amsterdam"), *local_day(2026, 3, 29))
    assert len(fires) == 1
    # 02:30 does not exist; it fires right after the jump (01:30 UTC = 03:30 CEST).
    assert fires[0].astimezone(UTC) == datetime(2026, 3, 29, 1, 30, tzinfo=UTC)


def test_spring_forward_hourly_has_23_fires() -> None:
    fires = fires_between(build_trigger("0 * * * *", "Europe/Amsterdam"), *local_day(2026, 3, 29))
    assert len(fires) == 23


def test_normal_day_fixed_hour() -> None:
    fires = fires_between(build_trigger("15 9 * * 1-5", "Europe/Amsterdam"), *local_day(2026, 6, 1))
    assert [f.astimezone(UTC) for f in fires] == [datetime(2026, 6, 1, 7, 15, tzinfo=UTC)]


def test_trigger_survives_pickle() -> None:
    trigger = build_trigger("30 2 * * *", "Europe/Amsterdam")
    clone = pickle.loads(pickle.dumps(trigger))  # noqa: S301 - our own data, like the jobstore
    assert isinstance(clone, DstSafeCronTrigger)
    assert len(fires_between(clone, *local_day(2026, 10, 25))) == 1


# --- latest_fire_time --------------------------------------------------------


def test_latest_fire_time_exact_and_late() -> None:
    trigger = build_trigger("* * * * *", "UTC")
    exact = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
    assert latest_fire_time(trigger, exact, timedelta(minutes=3)) == exact
    late = exact + timedelta(seconds=42)
    assert latest_fire_time(trigger, late, timedelta(minutes=3)) == exact


def test_latest_fire_time_none_within_lookback() -> None:
    trigger = build_trigger("0 0 1 1 *", "UTC")
    now = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
    assert latest_fire_time(trigger, now, timedelta(minutes=5)) is None


def test_latest_fire_time_respects_dst_rule() -> None:
    trigger = build_trigger("30 2 * * *", "Europe/Amsterdam")
    # During the second 02:30 (CET): the last valid firing is the one an hour earlier.
    now = datetime(2026, 10, 25, 1, 30, 5, tzinfo=UTC)
    assert latest_fire_time(trigger, now, timedelta(hours=2)) == datetime(
        2026, 10, 25, 0, 30, tzinfo=UTC
    )


# --- weekdagen: cron-nummering (0/7 = zondag) --------------------------------


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("*", "*"),
        ("0", "sun"),
        ("7", "sun"),
        ("1", "mon"),
        ("1-5", "mon,tue,wed,thu,fri"),
        ("MON-fri", "mon,tue,wed,thu,fri"),
        ("5-7", "sun,fri,sat"),
        ("*/2", "sun,tue,thu,sat"),
        ("1-5/2", "mon,wed,fri"),
        ("0,6", "sun,sat"),
    ],
)
def test_normalize_day_of_week(field: str, expected: str) -> None:
    assert normalize_day_of_week(field) == expected


def test_monday_means_monday() -> None:
    trigger = build_trigger("0 9 * * 1", "UTC")
    sunday = datetime(2026, 5, 31, 12, 0, tzinfo=UTC)
    nxt = trigger.get_next_fire_time(None, sunday)
    assert nxt is not None
    assert nxt.strftime("%a %Y-%m-%d %H:%M") == "Mon 2026-06-01 09:00"


# --- validatie ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("cron", "tz"),
    [
        ("* * * *", "UTC"),
        ("* * * * * *", "UTC"),
        ("61 * * * *", "UTC"),
        ("* * * * *", "Mars/Olympus"),
        ("* * * * *", "../../etc/passwd"),
        ("0 9 * * 8", "UTC"),
        ("0 9 * * 5-1", "UTC"),
        ("0 9 * * 1/2", "UTC"),
        ("0 9 1 * 1", "UTC"),
    ],
)
def test_invalid_schedule(cron: str, tz: str) -> None:
    with pytest.raises(InvalidScheduleError):
        build_trigger(cron, tz)
    with pytest.raises(ValidationError):
        ScheduleIn(template_id=1, cron=cron, timezone=tz)


def test_schedule_defaults() -> None:
    s = ScheduleIn(template_id=1, cron="*/5 * * * *")
    assert s.timezone == "UTC"
    assert s.overlap_policy == "skip"
    assert s.misfire_grace_s == 60
    assert s.enabled is True
