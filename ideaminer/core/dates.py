from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

_COMPACT_OFFSET = re.compile(r"([+-]\d{2})(\d{2})$")
_MILLISECONDS_FROM = 1e12


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def iso(dt: datetime | None) -> str | None:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat() if dt else None


def parse_dt(value: Any) -> datetime | None:
    """Parses ISO 8601, RFC 2822 and unix time (seconds or milliseconds) into an aware datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return _aware(value)
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > _MILLISECONDS_FROM:
            ts /= 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    s = str(value).strip()
    if s.isdigit():
        return parse_dt(int(s))
    s = _COMPACT_OFFSET.sub(r"\1:\2", s.replace("Z", "+00:00"))
    try:
        return _aware(datetime.fromisoformat(s))
    except ValueError:
        pass
    try:
        return _aware(parsedate_to_datetime(s))
    except (TypeError, ValueError):
        return None


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
