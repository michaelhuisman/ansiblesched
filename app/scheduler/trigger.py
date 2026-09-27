"""Cron-triggers met Vixie-cron-semantiek rond DST.

APScheduler's CronTrigger vuurt een job op een vast uur (bv. `30 2 * * *`) twee keer
als de klok in de herfst teruggaat. Vixie cron doet dat één keer:

- uurveld met wildcard of stap (`*`, `*/2`): gewoon elk uur, dus in het dubbele uur
  twee keer (er verstrijkt echt een uur);
- vast uurveld: de tweede keer dezelfde wandkloktijd wordt overgeslagen.

Een niet-bestaande tijd in het voorjaar (02:30 op de omschakeldag) vuurt direct na de
sprong, zoals APScheduler al doet.

Verder wijkt APScheduler's `from_crontab` af van cron:
- weekdagen zijn daar 0 = maandag; wij vertalen het veld naar namen met cron-nummering
  (0 en 7 = zondag);
- dag-van-de-maand en weekdag worden met EN gecombineerd i.p.v. OF; die combinatie
  weigeren we.

Let op: deze klasse wordt gepickled in de jobstore; module en naam niet verplaatsen.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.cron import CronTrigger


class InvalidScheduleError(ValueError):
    pass


def _is_repeated_wall_time(moment: datetime) -> bool:
    """True als dit de tweede keer is dat deze wandkloktijd voorkomt (herfst-DST)."""
    first = moment.replace(fold=0)
    # In UTC vergelijken: binnen dezelfde tzinfo negeert Python fold bij vergelijken.
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
    """Vertaal een cron-weekdagveld (0/7 = zondag) naar APScheduler-namen."""
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
    """Volgende afvuring, in UTC."""
    result: datetime | None = trigger.get_next_fire_time(None, now)
    return result.astimezone(UTC) if result is not None else None


def latest_fire_time(trigger: CronTrigger, now: datetime, lookback: timedelta) -> datetime | None:
    """Het laatste fire-moment <= now, binnen `lookback`.

    Deterministisch uit de trigger afgeleid, zodat twee schedulers voor dezelfde
    afvuring hetzelfde `scheduled_for` bepalen. Geeft UTC terug: `==` tussen
    tijdzones is altijd False bij een dubbelzinnige wandkloktijd (PEP 495).
    """
    latest: datetime | None = None
    candidate: datetime | None = trigger.get_next_fire_time(None, now - lookback)
    while candidate is not None and candidate <= now:
        latest = candidate
        candidate = trigger.get_next_fire_time(candidate, candidate)
    return latest.astimezone(UTC) if latest is not None else None
