"""Filteren van ansible-runner events vóór opslag.

Drie lagen:
1. no_log: resultaten met `_ansible_no_log` of een `censored`-markering worden vervangen
   en de stdout van het event vervalt.
2. Bekende secret-velden (password, token, private_key, ...) worden gemaskeerd.
3. Bekende secret-waarden (de credentials van deze run) worden overal gemaskeerd.
"""

import re
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

REDACTED = "********"
CENSORED = {"censored": "the output has been hidden due to the fact that 'no_log: true' was used"}

_SECRET_KEY = re.compile(
    r"password|passwd|passphrase|secret|token|private_key|api_key|(^|_)pass$",
    re.IGNORECASE,
)
# Kortere waarden maskeren geeft te veel valse treffers.
_MIN_SECRET_LEN = 6

# JSON uit ansible-runner: willekeurig geneste structuur.
Json = Any


@dataclass(frozen=True)
class FilteredEvent:
    seq: int
    event: str
    host: str | None
    task: str | None
    created_at: datetime
    stdout: str
    data: dict[str, Json]

    def as_row(self, run_id: int) -> dict[str, Json]:
        return {
            "run_id": run_id,
            "seq": self.seq,
            "event": self.event,
            "host": self.host,
            "task": self.task,
            "created_at": self.created_at,
            "stdout": self.stdout,
            "data": self.data,
        }


class SecretMasker:
    def __init__(self, secrets: Collection[str]) -> None:
        needles: set[str] = set()
        for secret in secrets:
            stripped = secret.strip()
            if len(stripped) >= _MIN_SECRET_LEN:
                needles.add(stripped)
            # Meerregelige secrets (SSH-keys) ook per regel, voor als ze deels in output komen.
            needles.update(
                line.strip() for line in secret.splitlines() if len(line.strip()) >= _MIN_SECRET_LEN
            )
        self._pattern = (
            re.compile("|".join(re.escape(n) for n in sorted(needles, key=len, reverse=True)))
            if needles
            else None
        )

    def mask(self, text: str) -> str:
        if self._pattern is None:
            return text
        return self._pattern.sub(REDACTED, text)


def _is_no_log(value: Mapping[str, Json]) -> bool:
    return bool(value.get("_ansible_no_log")) or "censored" in value


def _clean(value: Json, masker: SecretMasker, state: dict[str, bool]) -> Json:
    if isinstance(value, Mapping):
        if _is_no_log(value):
            state["no_log"] = True
            return dict(CENSORED)
        out: dict[str, Json] = {}
        for key, item in value.items():
            key_str = str(key)
            if _SECRET_KEY.search(key_str) and item not in (None, "", False):
                out[key_str] = REDACTED
            else:
                out[key_str] = _clean(item, masker, state)
        return out
    if isinstance(value, list | tuple):
        return [_clean(item, masker, state) for item in value]
    if isinstance(value, str):
        return _strip_nul(masker.mask(value))
    return value


def _strip_nul(text: str) -> str:
    # Postgres text/jsonb kan geen NUL-bytes bevatten.
    return text.replace("\x00", "")


def _parse_created(raw: Json) -> datetime:
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return datetime.now(UTC)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return datetime.now(UTC)


def filter_event(raw: Mapping[str, Json], masker: SecretMasker) -> FilteredEvent | None:
    """Zet een ruw ansible-runner event om naar een veilig op te slaan event.

    Geeft None voor events zonder volgnummer (die kunnen we niet ordenen).
    """
    counter = raw.get("counter")
    if not isinstance(counter, int):
        return None

    event_data = raw.get("event_data")
    state = {"no_log": False}
    data = _clean(event_data, masker, state) if isinstance(event_data, Mapping) else {}
    if isinstance(event_data, Mapping) and event_data.get("no_log"):
        state["no_log"] = True

    stdout = "" if state["no_log"] else _strip_nul(masker.mask(str(raw.get("stdout") or "")))

    host = data.get("host") or data.get("remote_addr")
    task = data.get("task")
    return FilteredEvent(
        seq=counter,
        event=str(raw.get("event") or "unknown"),
        host=str(host) if host else None,
        task=str(task) if task else None,
        created_at=_parse_created(raw.get("created")),
        stdout=stdout,
        data=data,
    )
