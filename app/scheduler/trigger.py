"""Cron triggers with Vixie cron semantics around DST.

APScheduler's CronTrigger fires a job at a fixed hour (e.g. `30 2 * * *`) twice when
the clock goes back in autumn. Vixie cron does it once:

- hour field with wildcard or step (`*`, `*/2`): simply every hour, so twice in the
  repeated hour (a real hour passes);
- fixed hour field: the second occurrence of the same wall-clock time is skipped.

A non-existent time in spring (02:30 on the transition day) fires right after the
jump, as APScheduler already does.

APScheduler's `from_crontab` also differs from cron:
- weekdays there are 0 = Monday; we translate the field to names with cron numbering
  (0 and 7 = Sunday);
- day of month and day of week are combined with AND instead of OR; we reject that
  combination.

Note: this class is pickled in the jobstore; do not move the module or rename it.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.cron import CronTrigger


class InvalidScheduleError(ValueError):
    pass


def _is_repeated_wall_time(moment: datetime) -> bool:
    """True if this is the second occurrence of this wall-clock time (autumn DST)."""
    first = moment.replace(fold=0)
    # Compare in UTC: within the same tzinfo Python ignores fold when comparing.
    return first.utcoffset() != moment.utcoffset() and first.astimezone(UTC) < moment.astimezone(
        UTC
    )


class DstSafeCronTrigger(CronTrigger):
    def _has_fixed_hour(self) -> bool:
        hour = next(f for f in self.fields if f.name == "hour")
        expr = str(hour)
        return "*" not in expr and "/" not in expr

    def get_next_fire_time(
        self, previous_fire_time: datetime | None, now: datetime
    ) -> datetime | None:
        nxt: datetime | None = super().get_next_fire_time(previous_fire_time, now)
        if self._has_fixed_hour():
            while nxt is not None and _is_repeated_wall_time(nxt):
                nxt = super().get_next_fire_time(nxt, nxt)
        return nxt


_DOW_NAMES = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")


def _dow_value(token: str) -> int:
    lowered = token.lower()
    if lowered in _DOW_NAMES:
        return _DOW_NAMES.index(lowered)
    if token.isdigit() and 0 <= int(token) <= 7:
        return int(token)
    raise InvalidScheduleError(f"invalid day of week: {token!r}")


def normalize_day_of_week(field: str) -> str:
    """Translate a cron weekday field (0/7 = Sunday) to APScheduler names."""
    if field == "*":
        return "*"
    days: set[int] = set()
    for part in field.split(","):
        base, _, step_text = part.partition("/")
        step = 1
        if step_text:
            if not step_text.isdigit() or int(step_text) == 0:
                raise InvalidScheduleError(f"invalid step in day of week: {part!r}")
            step = int(step_text)
        if base == "*":
            low, high = 0, 6
        elif "-" in base:
            start, _, end = base.partition("-")
            low, high = _dow_value(start), _dow_value(end)
        elif step_text:
            raise InvalidScheduleError(f"step needs a range in day of week: {part!r}")
        else:
            low = high = _dow_value(base)
        if low > high:
            raise InvalidScheduleError(f"invalid range in day of week: {part!r}")
        days.update(day % 7 for day in range(low, high + 1, step))
    return ",".join(_DOW_NAMES[day] for day in sorted(days))


def build_trigger(cron: str, timezone: str) -> DstSafeCronTrigger:
    try:
        tz = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise InvalidScheduleError(f"unknown timezone: {timezone!r}") from exc
    fields = cron.split()
    if len(fields) != 5:
        raise InvalidScheduleError("cron must have 5 fields: minute hour day month weekday")
    minute, hour, day, month, day_of_week = fields
    if day != "*" and day_of_week != "*":
        raise InvalidScheduleError(
            "restricting both day of month and day of week is not supported"
            " (cron combines them with OR); use two schedules"
        )
    try:
        trigger = DstSafeCronTrigger(
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=normalize_day_of_week(day_of_week),
            timezone=tz,
        )
    except ValueError as exc:
        raise InvalidScheduleError(f"invalid cron expression: {exc}") from exc
    return trigger


def next_fire_time(trigger: CronTrigger, now: datetime) -> datetime | None:
    """Next fire time, in UTC."""
    result: datetime | None = trigger.get_next_fire_time(None, now)
    return result.astimezone(UTC) if result is not None else None


def latest_fire_time(trigger: CronTrigger, now: datetime, lookback: timedelta) -> datetime | None:
    """The last fire time <= now, within `lookback`.

    Derived deterministically from the trigger, so two schedulers determine the same
    `scheduled_for` for the same firing. Returns UTC: `==` across time zones is always
    False for an ambiguous wall-clock time (PEP 495).
    """
    latest: datetime | None = None
    candidate: datetime | None = trigger.get_next_fire_time(None, now - lookback)
    while candidate is not None and candidate <= now:
        latest = candidate
        candidate = trigger.get_next_fire_time(candidate, candidate)
    return latest.astimezone(UTC) if latest is not None else None
