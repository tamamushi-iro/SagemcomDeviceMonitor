"""Presence analytics: interval math and local-time bucketing, with no I/O.

Times are UTC epoch seconds (ints). An *interval* is ``(start, end)`` with
``start < end``; an interval list is sorted and non-overlapping. Everything here
is pure, so it is unit-tested without a database or a router.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo

Interval = tuple[int, int]

#: Successful polls further apart than this mean the monitor was not watching, so
#: what devices did in between is unknown (not "offline", not "online").
SAMPLE_GAP_S = 300

#: A device that reconnects within this long is treated as having stayed. Sleeping
#: phones and laptops leave Wi-Fi for long stretches between brief check-ins (a
#: napping MacBook shows up for seconds once an hour), so raw connection time
#: badly understates "is it here".
PRESENCE_GAP_S = 90 * 60

#: Offline for at most this long between two connections counts as a short drop.
SHORT_DROP_S = 5 * 60

#: Two MAC addresses with the same name that were online together for longer than
#: this are different physical devices; below it they are one device whose address
#: changed (MAC rotation hands over within minutes, with no real overlap).
SAME_DEVICE_OVERLAP_S = 10 * 60

BUCKET_S = 900  # width of the count rollup buckets


# -- interval lists ------------------------------------------------------------


def online_intervals(
    first_seen: int, is_active: bool, events: Sequence[tuple[int, bool]], end: int
) -> list[Interval]:
    """Online intervals over a device's whole history.

    ``events`` are ``(ts, went_online)`` transitions in time order. The state before
    the first event is its opposite: a device whose first event is "offline" was
    online since ``first_seen``. A device with no events has been in its current
    state all along. An interval still open at the end is closed at ``end``.
    """
    out: list[Interval] = []
    online = (not events[0][1]) if events else is_active
    start = first_seen if online else 0
    for ts, to_online in events:
        if to_online and not online:
            online, start = True, ts
        elif not to_online and online:
            online = False
            if ts > start:
                out.append((start, ts))
    if online and end > start:
        out.append((start, end))
    return out


def intersect(a: Sequence[Interval], b: Sequence[Interval]) -> list[Interval]:
    """The parts covered by both interval lists."""
    out: list[Interval] = []
    i = j = 0
    while i < len(a) and j < len(b):
        lo, hi = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if lo < hi:
            out.append((lo, hi))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def bridge(ivs: Iterable[Interval], tolerance: int = 0) -> list[Interval]:
    """Merge intervals separated by gaps of at most ``tolerance`` seconds."""
    out: list[Interval] = []
    for s, e in ivs:
        if out and s - out[-1][1] <= tolerance:
            if e > out[-1][1]:
                out[-1] = (out[-1][0], e)
        else:
            out.append((s, e))
    return out


def union(lists: Iterable[Sequence[Interval]]) -> list[Interval]:
    """Everything covered by at least one of the interval lists."""
    return bridge(sorted(iv for ivs in lists for iv in ivs))


def complement(ivs: Sequence[Interval], a: int, b: int) -> list[Interval]:
    """The parts of ``[a, b]`` not covered by ``ivs``."""
    out: list[Interval] = []
    cur = a
    for s, e in ivs:
        if e <= a:
            continue
        if s >= b:
            break
        if s > cur:
            out.append((cur, s))
        cur = max(cur, e)
    if cur < b:
        out.append((cur, b))
    return out


def overlap_exceeds(a: Sequence[Interval], b: Sequence[Interval], limit: int) -> bool:
    """True once the two lists overlap for more than ``limit`` seconds in total."""
    if not a or not b or a[-1][1] <= b[0][0] or b[-1][1] <= a[0][0]:
        return False
    total = i = j = 0
    while i < len(a) and j < len(b):
        lo, hi = max(a[i][0], b[j][0]), min(a[i][1], b[j][1])
        if lo < hi:
            total += hi - lo
            if total > limit:
                return True
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return False


class Track:
    """An interval list with fast "how much of [a, b] is covered" queries."""

    __slots__ = ("ivs", "_starts", "_ends", "_cum")

    def __init__(self, ivs: Iterable[Interval] = ()):
        self.ivs: list[Interval] = list(ivs)
        self._starts = [s for s, _ in self.ivs]
        self._ends = [e for _, e in self.ivs]
        cum = [0]
        for s, e in self.ivs:
            cum.append(cum[-1] + e - s)
        self._cum = cum

    def __bool__(self) -> bool:
        return bool(self.ivs)

    @property
    def total(self) -> int:
        return self._cum[-1]

    def _before(self, t: int) -> int:
        """Seconds covered before time ``t``."""
        i = bisect_right(self._starts, t)
        if i == 0:
            return 0
        s, e = self.ivs[i - 1]
        return self._cum[i - 1] + min(e, t) - s

    def overlap(self, a: int, b: int) -> int:
        """Seconds of ``[a, b]`` that are covered."""
        return self._before(b) - self._before(a) if b > a else 0

    def at(self, t: int) -> bool:
        """Is ``t`` inside an interval (start inclusive, end exclusive)?"""
        i = bisect_right(self._starts, t) - 1
        return i >= 0 and t < self._ends[i]

    def slice(self, a: int, b: int) -> list[Interval]:
        """The intervals, clipped to ``[a, b]``."""
        out: list[Interval] = []
        i = bisect_right(self._ends, a)
        while i < len(self.ivs) and self.ivs[i][0] < b:
            s, e = self.ivs[i]
            out.append((max(s, a), min(e, b)))
            i += 1
        return out


def short_drops(raw: Track, cover: Track, a: int, b: int, max_gap: int = SHORT_DROP_S) -> int:
    """Count brief disconnections in ``[a, b]``: the device was connected, vanished
    for at most ``max_gap`` seconds, and came back, all while being monitored."""
    ivs = raw.slice(a, b)
    n = 0
    for (_, e1), (s2, _) in zip(ivs, ivs[1:]):
        gap = s2 - e1
        if 0 < gap <= max_gap and cover.overlap(e1, s2) == gap:
            n += 1
    return n


# -- local time ----------------------------------------------------------------


def local_date(tz: ZoneInfo, t: int) -> date:
    return datetime.fromtimestamp(t, tz).date()


@lru_cache(maxsize=8192)
def day_start(tz: ZoneInfo, d: date) -> int:
    """Epoch of local midnight at the start of day ``d``."""
    return int(datetime(d.year, d.month, d.day, tzinfo=tz).timestamp())


@lru_cache(maxsize=8192)
def half_hour_edges(tz: ZoneInfo, d: date) -> tuple[int, ...]:
    """The 49 epoch edges of the 48 wall-clock half-hours of local day ``d``.

    On the two days a year the clocks change, cells around the jump are longer,
    shorter or empty; the edges stay in order either way.
    """
    edges = [
        int(datetime(d.year, d.month, d.day, h, m, tzinfo=tz).timestamp())
        for h in range(24)
        for m in (0, 30)
    ]
    edges.append(day_start(tz, d + timedelta(days=1)))
    for i in range(1, len(edges)):
        if edges[i] < edges[i - 1]:
            edges[i] = edges[i - 1]
    return tuple(edges)


def wall_minutes(tz: ZoneInfo, t: int) -> float:
    """Minutes since local midnight on the wall clock at instant ``t``."""
    dt = datetime.fromtimestamp(t, tz)
    return dt.hour * 60 + dt.minute + dt.second / 60


def day_spans(ivs: Sequence[Interval], tz: ZoneInfo, d: date) -> list[list[float]]:
    """``ivs`` (already clipped to day ``d``) as wall-clock ``[from, to]`` minutes."""
    a, b = day_start(tz, d), day_start(tz, d + timedelta(days=1))
    out: list[list[float]] = []
    for s, e in ivs:
        m0 = 0.0 if s <= a else wall_minutes(tz, s)
        m1 = 1440.0 if e >= b else wall_minutes(tz, e)
        if m1 < m0:  # inside the repeated hour when the clocks go back
            m1 = m0
        out.append([round(m0, 1), round(m1, 1)])
    return out


def days_between(first: date, last: date) -> list[date]:
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def longest_low_run(profile: Sequence[float | None], below: float = 0.5) -> tuple[int, int] | None:
    """The longest run of cells under ``below`` in a circular daily profile.

    Returns ``(start, length)`` in cells; ``None`` if no cell is under the line.
    A run may wrap past midnight. Cells with no data break a run.
    """
    n = len(profile)
    low = [p is not None and p < below for p in profile]
    if not any(low):
        return None
    if all(low):
        return (0, n)
    best = (0, 0)
    # Start scanning just after a cell that is not low, so a wrapping run is whole.
    first = next(i for i in range(n) if not low[i])
    run_start, run_len = 0, 0
    for k in range(1, n + 1):
        i = (first + k) % n
        if low[i]:
            if run_len == 0:
                run_start = i
            run_len += 1
            if run_len > best[1]:
                best = (run_start, run_len)
        else:
            run_len = 0
    return best if best[1] else None


# -- identity ------------------------------------------------------------------

_MAC_RE = re.compile(r"^(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}$")
_ANDROID_POSSESSIVE = re.compile(r"^([A-Za-z][A-Za-z0-9]{1,23})-s-\w")
_APPLE_POSSESSIVE = re.compile(
    r"^([A-Za-z][A-Za-z0-9]{1,23}?)s-(?:iPhone|iPad|iPod|MacBook|Mac|iMac|Watch|AirPods)",
    re.IGNORECASE,
)


_PORTABLE = re.compile(
    r"iphone|watch|pixel|galaxy|android|oneplus|redmi|xiaomi|huawei|oppo|vivo|moto|nokia|phone"
    r"|[-_ ][samz]\d{2}(?!\d)|note\d|fold|flip",
    re.IGNORECASE,
)
_STAYS_PUT = re.compile(r"tab|pad|book|laptop|desktop|tv", re.IGNORECASE)


def looks_like_mac(text: str) -> bool:
    return bool(_MAC_RE.match(text))


def looks_portable(name: str | None) -> bool:
    """Does the hostname read like a phone or a watch: something that goes where
    its owner goes, as opposed to a laptop that sleeps on a desk all day?"""
    return bool(name) and bool(_PORTABLE.search(name)) and not _STAYS_PUT.search(name)


def is_randomized_mac(mac: str) -> bool:
    """Locally administered ("private") address: second hex digit is 2, 6, A or E."""
    return len(mac) > 1 and mac[1].upper() in "26AE"


def suggest_owner(name: str | None) -> str | None:
    """A person's name embedded in a hostname, e.g. "Manan-s-S25-Ultra" or
    "Sabis-iPhone" (how phones spell "Manan's S25 Ultra" and "Sabi's iPhone")."""
    if not name:
        return None
    m = _ANDROID_POSSESSIVE.match(name) or _APPLE_POSSESSIVE.match(name)
    if not m:
        return None
    who = m.group(1)
    return who[0].upper() + who[1:]


def single_device_names(
    names: dict[str, list[str]], tracks: dict[str, Track]
) -> set[str]:
    """Names that behave like one physical device.

    ``names`` maps a hostname to the MAC addresses that used it. Phones that rotate
    their private address show up under many MACs, one after another; several
    different phones that share a default name ("iPhone") are online together.
    A name qualifies when no two of its addresses were online at the same time.
    """
    single: set[str] = set()
    for name, macs in names.items():
        if len(macs) < 2:
            continue
        # Busiest first: genuinely different devices then collide on the first pairs.
        live = sorted((tracks[m] for m in macs if tracks[m]), key=lambda t: -t.total)
        clash = any(
            overlap_exceeds(live[i].ivs, live[j].ivs, SAME_DEVICE_OVERLAP_S)
            for i in range(len(live))
            for j in range(i + 1, len(live))
        )
        if not clash:
            single.add(name)
    return single


# -- behaviour -----------------------------------------------------------------

KINDS = ("always_on", "regular", "occasional", "new", "gone", "never")


def classify(
    *,
    now: int,
    first_on: int | None,
    last_on: int | None,
    online_s: int,
    observed_s: int,
    recent_days_seen: int,
    recent_days_observed: int,
) -> str:
    """Sort a device into a behaviour class from its history.

    ``observed_s`` is the monitored time since the device first connected and
    ``online_s`` how much of it the device was connected. The "recent" pair counts
    local days in the last four weeks.
    """
    if first_on is None or last_on is None:
        return "never"
    if observed_s >= 3 * 86400 and online_s >= 0.9 * observed_s:
        return "always_on"
    if now - last_on > 30 * 86400:
        return "gone"
    if now - first_on < 14 * 86400:
        return "new"
    if recent_days_observed and recent_days_seen >= 0.5 * recent_days_observed:
        return "regular"
    return "occasional"


def usual_band(values: Sequence[float]) -> tuple[float, float, float] | None:
    """``(low, middle, high)`` of what is normal, from the same slot in past weeks.

    With enough weeks the single highest and lowest are dropped, so one party or
    one outage does not stretch "usual".
    """
    vals = sorted(values)
    if len(vals) < 3:
        return None
    mid = vals[len(vals) // 2] if len(vals) % 2 else (vals[len(vals) // 2 - 1] + vals[len(vals) // 2]) / 2
    if len(vals) >= 6:
        return vals[1], mid, vals[-2]
    return vals[0], mid, vals[-1]
