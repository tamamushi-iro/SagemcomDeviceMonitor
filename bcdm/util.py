"""Small shared helpers: UTC time formatting and human-friendly durations."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from dateutil import parser
from dateutil.relativedelta import relativedelta

# All timestamps are stored in SQLite in this UTC format, which SQLite's
# strftime() understands natively (no 'T', no offset -- implicitly UTC).
TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_str() -> str:
    return utc_now().strftime(TS_FORMAT)


def since_str(**delta: float) -> str:
    """A stored-format timestamp `delta` in the past, e.g. since_str(days=30)."""
    return (utc_now() - timedelta(**delta)).strftime(TS_FORMAT)


def parse_ts(ts: str) -> datetime:
    """Parse a stored 'YYYY-MM-DD HH:MM:SS' UTC string to an aware datetime."""
    return datetime.strptime(ts, TS_FORMAT).replace(tzinfo=timezone.utc)


def to_iso_z(ts: str | None) -> str | None:
    """Convert a stored 'YYYY-MM-DD HH:MM:SS' UTC string to ISO8601 'Z' form
    so the browser parses it as UTC and renders it in the viewer's local time."""
    if not ts:
        return None
    return ts.replace(" ", "T") + "Z"


def x_time_ago(active_last_change_str: str | None) -> str | None:
    """Human-friendly 'X ago' for the router's active_last_change timestamp.

    Ported verbatim (behaviour-wise) from the original bcdm_v2.py.
    """
    if not active_last_change_str:
        return None

    active_last_change = parser.isoparse(active_last_change_str)
    now = datetime.now(timezone.utc)
    rel = relativedelta(now, active_last_change)

    out = ""
    if rel.years > 0:
        out += f"{rel.years} years "
    if rel.months > 0:
        out += f"{rel.months} months "
    if rel.days > 0:
        out += f"{rel.days} days "
    if rel.hours > 0:
        out += f"{rel.hours} hours "
    if rel.minutes > 0:
        out += f"{rel.minutes} mins "
    if rel.seconds > 0:
        out += f"{rel.seconds} secs"

    out = out.strip()
    return (out + " ago") if out else "just now"
