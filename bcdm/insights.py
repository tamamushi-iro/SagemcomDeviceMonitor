"""Turn stored presence data into the dashboard's JSON payloads.

A :class:`State` is a snapshot of everything needed, loaded by the Database under
its lock. :func:`build_model` groups MAC addresses into devices and people, and
the public functions (``overview``, ``patterns``, ``devices``, ``device_days``,
``health``) each build one page's payload from that model.

Payload times are UTC epoch seconds. Positions on a day are minutes since local
midnight, in the timezone the browser asked for.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, Iterable
from zoneinfo import ZoneInfo

from .analytics import (
    BUCKET_S,
    PRESENCE_GAP_S,
    SHORT_DROP_S,
    Interval,
    Track,
    bridge,
    classify,
    complement,
    day_spans,
    day_start,
    days_between,
    half_hour_edges,
    intersect,
    is_randomized_mac,
    local_date,
    longest_low_run,
    looks_like_mac,
    looks_portable,
    online_intervals,
    short_drops,
    single_device_names,
    suggest_owner,
    union,
    usual_band,
    wall_minutes,
)

OVERVIEW_WINDOW_S = 48 * 3600
USUAL_WEEKS = 8
#: Visits shorter than this stay out of the activity feed: a sleeping phone checking
#: in for a minute is neither an arrival nor a departure.
MIN_VISIT_S = 10 * 60
KIND_ORDER = {"regular": 0, "occasional": 1, "new": 2, "always_on": 3, "gone": 4, "never": 5}


class UnknownSubject(KeyError):
    """Raised when a patterns subject does not exist (any more)."""


@dataclass
class State:
    """Raw material for one request, as loaded from the database."""

    now: int
    sample_gap: int
    cover: list[Interval]  # spans in which the monitor was polling
    devices: list[dict[str, Any]]  # one per MAC; first_seen / last_seen are epochs
    events: dict[str, list[tuple[int, bool]]]  # mac -> (ts, went_online)
    rollup: dict[int, tuple[int, int, int, int]]  # 15-min bucket -> (n, sum, min, max)
    version: int = 0  # changes whenever a presence event is recorded
    memo: dict[Any, Any] = field(default_factory=dict)  # scratch cache kept by the Database


@dataclass
class Entity:
    """A device as a person thinks of it: one or more MAC addresses."""

    key: str
    base: str  # label, hostname or MAC, before disambiguation
    name: str  # display name, unique among devices
    hostname: str | None
    label: str | None
    owner: str | None
    rows: list[dict[str, Any]]  # member MAC rows, most recently seen first
    raw: Track  # connected
    home: Track  # connected, with short gaps bridged
    home_cov: Track  # ``home`` restricted to monitored time
    active: bool
    hidden: bool
    watched: bool
    first_on: int | None
    last_on: int | None
    carried_set: bool | None = None  # what the person chose; None = decide from the hostname
    kind: str = "never"
    uptime: float | None = None

    @property
    def mac(self) -> str:
        return self.rows[0]["mac"]

    @property
    def carried(self) -> bool:
        """Does it go where its owner goes? Only such devices say whether a person
        is home: a laptop left on the desk would make its owner look home all day."""
        if self.carried_set is not None:
            return self.carried_set
        return self.kind != "always_on" and looks_portable(self.hostname)

    @property
    def suggestion(self) -> str | None:
        """An owner its hostname hints at, for devices that have actually been here."""
        return None if self.owner or not self.raw else suggest_owner(self.hostname)


@dataclass
class Person:
    name: str
    entities: list[Entity]  # everything they own
    tracking: list[Entity]  # the devices their presence is read from
    raw: Track
    home: Track
    home_cov: Track

    @property
    def is_person(self) -> bool:
        """An owner of nothing but always-on gear ("House") is not someone who comes and goes."""
        return any(e.kind not in ("always_on", "never") for e in self.tracking)


@dataclass
class Model:
    tz: ZoneInfo
    now: int
    last_sample: int
    sample_gap: int
    cover: Track
    rollup: dict[int, tuple[int, int, int, int]]
    entities: dict[str, Entity]
    people: dict[str, Person]
    data_start: int

    @property
    def stale(self) -> bool:
        """No successful poll for a while: the latest state is no longer current."""
        return self.now - self.last_sample > 2 * self.sample_gap

    @property
    def today(self) -> date:
        return local_date(self.tz, self.now)

    @property
    def first_day(self) -> date:
        return local_date(self.tz, self.data_start) if self.data_start else self.today


# -- model ---------------------------------------------------------------------


def build_model(st: State, tz: ZoneInfo) -> Model:
    cover_ivs = [(s, min(e, st.now)) for s, e in st.cover if s < st.now and min(e, st.now) > s]
    cover = Track(cover_ivs)
    last_sample = cover_ivs[-1][1] if cover_ivs else 0
    data_start = cover_ivs[0][0] if cover_ivs else 0

    rows = [d for d in st.devices if d["first_seen"] <= st.now]
    tracks: dict[str, Track] = {}
    for d in rows:
        raw = online_intervals(
            d["first_seen"], bool(d["is_active"]), st.events.get(d["mac"], ()), last_sample
        )
        track = tracks[d["mac"]] = Track(intersect(raw, cover_ivs))
        d["on_now"] = bool(track) and track.ivs[-1][1] >= last_sample > 0

    # Hostnames that behave like a single physical device get merged into one.
    by_name: dict[str, list[str]] = defaultdict(list)
    for d in rows:
        host = (d["name"] or "").strip()
        if host and not looks_like_mac(host):
            by_name[host.lower()].append(d["mac"])
    memo_key = ("single", st.version, len(rows), last_sample // 3600)
    single = st.memo.get(memo_key)
    if single is None:
        single = single_device_names(by_name, tracks)
        st.memo.clear()
        st.memo[memo_key] = single

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for d in rows:
        host = (d["name"] or "").strip().lower()
        key = f"name:{host}" if host in single else f"mac:{d['mac']}"
        groups[key].append(d)

    def last_online(d: dict[str, Any]) -> int:
        track = tracks[d["mac"]]
        return track.ivs[-1][1] if track else 0

    recent_days = days_between(local_date(tz, st.now) - timedelta(days=27), local_date(tz, st.now))
    entities: dict[str, Entity] = {}
    for key, members in groups.items():
        # The address in use first. (last_seen only says the router still lists it.)
        members.sort(key=lambda d: (last_online(d), d["last_seen"]), reverse=True)
        raw = Track(union(tracks[d["mac"]].ivs for d in members)) if len(members) > 1 else tracks[members[0]["mac"]]
        home = Track(bridge(raw.ivs, PRESENCE_GAP_S))
        hostname = next((d["name"].strip() for d in members if (d["name"] or "").strip()), None)
        label = next((d["label"].strip() for d in members if (d["label"] or "").strip()), None)
        owner = next((d["owner"].strip() for d in members if (d.get("owner") or "").strip()), None)
        carried = next((bool(d["carried"]) for d in members if d.get("carried") is not None), None)
        ent = Entity(
            key=key,
            base=label or hostname or members[0]["mac"],
            name="",
            hostname=hostname,
            label=label,
            owner=owner,
            rows=members,
            raw=raw,
            home=home,
            home_cov=Track(intersect(home.ivs, cover_ivs)),
            active=bool(raw) and raw.ivs[-1][1] >= last_sample > 0,
            hidden=any(d["hidden"] for d in members),
            watched=any(d["watched"] for d in members),
            first_on=raw.ivs[0][0] if raw else None,
            last_on=raw.ivs[-1][1] if raw else None,
            carried_set=carried,
        )
        if raw:
            observed = cover.overlap(ent.first_on, last_sample)
            ent.uptime = raw.total / observed if observed else None
            seen = observed_days = 0
            for day in recent_days:
                a, b = day_start(tz, day), day_start(tz, day + timedelta(days=1))
                if cover.overlap(a, b) >= 3600:
                    observed_days += 1
                    seen += raw.overlap(a, b) > 0
            ent.kind = classify(
                now=last_sample,
                first_on=ent.first_on,
                last_on=ent.last_on,
                online_s=raw.total,
                observed_s=observed,
                recent_days_seen=seen,
                recent_days_observed=observed_days,
            )
        entities[key] = ent

    # Several devices can share a name ("iPhone"): tell them apart by address.
    shared = Counter(e.base.lower() for e in entities.values())
    for e in entities.values():
        e.name = f"{e.base} ·{e.mac[-5:]}" if shared[e.base.lower()] > 1 else e.base

    people: dict[str, Person] = {}
    owned: dict[str, list[Entity]] = defaultdict(list)
    for e in entities.values():
        if e.owner:
            owned[e.owner].append(e)
    for name, ents in owned.items():
        # Read presence from what they carry; with nothing like that, from all they own.
        tracking = [e for e in ents if e.carried and e.raw] or ents
        raw = Track(union(e.raw.ivs for e in tracking))
        home = Track(bridge(raw.ivs, PRESENCE_GAP_S))
        people[name] = Person(name, ents, tracking, raw, home, Track(intersect(home.ivs, cover_ivs)))

    return Model(
        tz=tz,
        now=st.now,
        last_sample=last_sample,
        sample_gap=st.sample_gap,
        cover=cover,
        rollup=st.rollup,
        entities=entities,
        people=people,
        data_start=data_start,
    )


# -- shared helpers --------------------------------------------------------------


def _lists(ivs: Iterable[Interval]) -> list[list[int]]:
    return [[s, e] for s, e in ivs]


def _status(m: Model, raw: Track, home: Track) -> dict[str, Any]:
    """Where a device or person stands at the latest poll."""
    if not raw:
        return {"state": "never", "since": None, "last_seen": None, "approx": False}
    last_end = raw.ivs[-1][1]
    session_start = home.ivs[-1][0]
    starts = {s for s, _ in m.cover.ivs}
    ends = {e for _, e in m.cover.ivs[:-1]}
    if last_end >= m.last_sample:
        # "Since at least": monitoring began or resumed with it already connected.
        return {"state": "home", "since": session_start, "last_seen": m.last_sample,
                "approx": session_start in starts}
    if m.last_sample - last_end <= PRESENCE_GAP_S:
        return {"state": "recent", "since": session_start, "last_seen": last_end,
                "approx": session_start in starts}
    # Still connected when monitoring stopped: it left some time during the gap.
    return {"state": "away", "since": last_end, "last_seen": last_end, "approx": last_end in ends}


def _bucket_mean(m: Model, a: int, b: int) -> tuple[float, int, int] | None:
    """Mean / min / max connected-device count over ``[a, b)`` from the rollup."""
    n = total = 0
    lo, hi = 10**9, -1
    for bucket in range(a // BUCKET_S, -(-b // BUCKET_S)):
        cell = m.rollup.get(bucket)
        if cell:
            n += cell[0]
            total += cell[1]
            lo, hi = min(lo, cell[2]), max(hi, cell[3])
    return (total / n, lo, hi) if n else None


def _usual(m: Model, hour_start: int) -> tuple[float, float, float] | None:
    """What is normal for this hour: the same weekday and hour in recent weeks."""
    local = datetime.fromtimestamp(hour_start, m.tz)
    values = []
    for k in range(1, USUAL_WEEKS + 1):
        prior = int((local - timedelta(days=7 * k)).timestamp())  # same wall-clock time
        cell = _bucket_mean(m, prior, prior + 3600)
        if cell:
            values.append(cell[0])
    band = usual_band(values)
    return tuple(round(v, 1) for v in band) if band else None  # type: ignore[return-value]


def _subject_tracks(
    m: Model, key: str
) -> tuple[str, str, Track, Track, Track, list[Entity], list[Entity]]:
    """Resolve "owner:<name>" / "dev:<device key>" to
    (kind, name, raw, home, home_cov, everything it covers, what its presence is read from)."""
    if key.startswith("owner:"):
        p = m.people.get(key[6:])
        if p is None:
            raise UnknownSubject(key)
        return "person", p.name, p.raw, p.home, p.home_cov, p.entities, p.tracking
    if key.startswith("dev:"):
        e = m.entities.get(key[4:])
        if e is None:
            raise UnknownSubject(key)
        return "device", e.name, e.raw, e.home, e.home_cov, [e], [e]
    raise UnknownSubject(key)


def _entity_brief(e: Entity) -> dict[str, Any]:
    return {
        "key": f"dev:{e.key}",
        "name": e.name,
        "kind": e.kind,
        "owner": e.owner,
        "carried": e.carried,
        "macs": len(e.rows),
        "active": e.active,
        "last_seen": e.last_on,
    }


# -- overview --------------------------------------------------------------------


def overview(m: Model, live: dict[str, Any] | None = None) -> dict[str, Any]:
    """Who is here now, the last two days, and whether that is normal."""
    t1 = m.now
    t0 = t1 - OVERVIEW_WINDOW_S
    today0 = day_start(m.tz, m.today)

    def row(key: str, name: str, sub: str, raw: Track, home: Track, home_cov: Track) -> dict[str, Any]:
        return {
            "key": key,
            "name": name,
            "sub": sub,
            **_status(m, raw, home),
            "on": _lists(raw.slice(t0, t1)),
            "home": _lists(home.slice(t0, t1)),
            "today_s": home_cov.overlap(today0, t1),
        }

    people = []
    for p in m.people.values():
        if not p.is_person:
            continue
        here = [e.name for e in p.tracking if e.active]
        sub = ", ".join(here) if here else f"{len(p.tracking)} device{'s' if len(p.tracking) != 1 else ''}"
        people.append(row(f"owner:{p.name}", p.name, sub, p.raw, p.home, p.home_cov))

    others = []
    for e in m.entities.values():
        if e.owner or e.hidden or e.kind in ("always_on", "never"):
            continue
        if e.home.overlap(t0, t1) <= 0:
            continue
        sub = "new device" if e.kind == "new" else ""
        others.append(row(f"dev:{e.key}", e.name, sub, e.raw, e.home, e.home_cov))

    rank = {"home": 0, "recent": 1, "away": 2, "never": 3}
    for rows in (people, others):
        rows.sort(key=lambda r: (rank[r["state"]], -(r["last_seen"] or 0), r["name"].lower()))
    others_more = max(0, len(others) - 12)
    others = others[:12]

    # Connected-device count against what is usual for these hours.
    points = []
    for bucket in range(t0 // BUCKET_S, t1 // BUCKET_S + 1):
        cell = m.rollup.get(bucket)
        if bucket * BUCKET_S <= m.last_sample:
            points.append([bucket * BUCKET_S, round(cell[1] / cell[0], 2) if cell else None])
    usual = []
    for hour in range(t0 // 3600, t1 // 3600 + 2):
        band = _usual(m, hour * 3600)
        if band:
            usual.append([hour * 3600, *band])

    active_macs = [(e, d) for e in m.entities.values() for d in e.rows if d["on_now"]]
    router_since = {
        d["mac"]: d.get("active_last_change_ts")
        for d in (live or {}).get("devices", [])
        if d.get("active_last_change_ts")
    }
    starts = {s for s, _ in m.cover.ivs}
    connected = []
    for e, d in active_macs:
        since = e.home.ivs[-1][0]  # here since, not counting brief drops
        connected.append({
            "key": f"dev:{e.key}",
            "name": e.name,
            "owner": e.owner,
            "mac": d["mac"],
            "ip": d["last_ip"],
            "interface": d["interface_type"],
            "randomized": is_randomized_mac(d["mac"]),
            "since": since,
            "since_approx": since in starts,
            "router_since": router_since.get(d["mac"]),
            "kind": e.kind,
            "hidden": bool(d["hidden"]),
            "watched": bool(d["watched"]),
            "is_new": e.first_on is not None and t1 - e.first_on < 7 * 86400 and e.first_on > m.data_start + 86400,
        })
    connected.sort(key=lambda r: (-r["since"], r["name"].lower()))

    seen = [e for e in m.entities.values() if e.raw]
    new = [e for e in seen if t1 - e.first_on < 7 * 86400 and e.first_on > m.data_start + 86400]
    always = [e for e in seen if e.kind == "always_on"]
    persons = [p for p in m.people.values() if p.is_person]
    suggestions = Counter(e.suggestion for e in m.entities.values() if e.suggestion)

    return {
        "now": t1,
        "tz": m.tz.key,
        "last_sample": m.last_sample or None,
        "stale": m.stale,
        "has_data": bool(m.cover),
        "counts": {
            "connected": len(active_macs),
            "usual": _usual(m, t1 // 3600 * 3600),
            "people_home": sum(1 for r in people if r["state"] in ("home", "recent")),
            "people_total": len(persons),
            "new_7d": len(new),
            "new_online": sum(1 for e in new if e.active),
            "always_on": len(always),
            "always_on_offline": sum(1 for e in always if not e.active),
            "devices_seen": len(seen),
        },
        "window": {
            "start": t0,
            "end": t1,
            "gaps": _lists(g for g in complement(m.cover.slice(t0, t1), t0, t1) if g[1] - g[0] >= m.sample_gap),
        },
        "people": people,
        "others": others,
        "others_more": others_more,
        "series": {"step": BUCKET_S, "points": points, "usual": usual},
        "connected": connected,
        "activity": _activity(m, t0, t1),
        "owner_suggestions": [{"owner": o, "devices": n} for o, n in suggestions.most_common()],
    }


def _activity(m: Model, t0: int, t1: int, limit: int = 20) -> list[dict[str, Any]]:
    """Recent arrivals and departures, with short gaps bridged so a napping phone
    does not flood the list. People count once, however many devices they carry."""
    starts = {s for s, _ in m.cover.ivs}
    ends = {e for _, e in m.cover.ivs}
    subjects: list[tuple[str, str, Track, int | None]] = [
        (f"owner:{p.name}", p.name, p.home, None) for p in m.people.values() if p.is_person
    ]
    subjects += [
        (f"dev:{e.key}", e.name, e.home, e.first_on)
        for e in m.entities.values()
        if not e.owner and not e.hidden and e.kind not in ("always_on", "never")
    ]
    out = []
    for key, name, home, first_on in subjects:
        ivs = home.ivs
        for i, (s, e) in enumerate(ivs):
            if e < t0:
                continue
            is_new = first_on == s and s > m.data_start + 86400
            if e - s < MIN_VISIT_S and e < m.last_sample and not is_new:
                continue
            # An arrival exactly when monitoring resumed has an unknown real time;
            # only a never-seen device is worth reporting then.
            if t0 <= s <= t1 and (is_new or s not in starts):
                out.append({
                    "t": s, "key": key, "name": name,
                    "type": "new" if is_new else "arrived",
                    "approx": s in starts,
                    "after_s": None if is_new or i == 0 else s - ivs[i - 1][1],
                })
            if t0 <= e <= t1 and e not in ends and m.last_sample - e > PRESENCE_GAP_S:
                out.append({"t": e, "key": key, "name": name, "type": "left",
                            "approx": False, "after_s": e - s})
    out.sort(key=lambda a: -a["t"])
    return out[:limit]


# -- patterns --------------------------------------------------------------------


def patterns(m: Model, subject: str, weeks: int) -> dict[str, Any]:
    """Typical week, day-by-day detail and daily totals for one subject.

    ``subject`` is ``devices`` (connected-device count), ``people`` (people at home),
    ``owner:<name>`` or ``dev:<device key>``. ``weeks`` of 0 means all history.
    """
    last = m.today
    first = m.first_day if not weeks else max(m.first_day, last - timedelta(days=weeks * 7 - 1))
    days = days_between(first, last)

    persons = sorted((p for p in m.people.values() if p.is_person), key=lambda p: p.name.lower())
    if subject == "people" and not persons:
        subject = "devices"

    out: dict[str, Any] = {
        "now": m.now,
        "now_min": round(wall_minutes(m.tz, m.now), 1),
        "tz": m.tz.key,
        "weeks": weeks,
        "start": first.isoformat(),
        "end": last.isoformat(),
        "first_day": m.first_day.isoformat(),
        "presence_gap_s": PRESENCE_GAP_S,
        "options": {
            "counts": [{"key": "devices", "name": "All devices"}]
            + ([{"key": "people", "name": "People at home"}] if persons else []),
            "people": [{"key": f"owner:{p.name}", "name": p.name} for p in persons],
            "devices": [
                _entity_brief(e)
                for e in sorted(
                    (e for e in m.entities.values() if e.raw),
                    key=lambda e: (KIND_ORDER[e.kind], e.name.lower()),
                )
            ],
        },
    }

    if subject in ("devices", "people"):
        # cell(a, b) -> (mean, lowest, highest) over a stretch of time, or None if unmonitored.
        if subject == "devices":
            def cell(a: int, b: int) -> tuple[float, float, float] | None:
                return _bucket_mean(m, a, b)
            name, unit = "All devices", "devices"
        else:
            def cell(a: int, b: int) -> tuple[float, float, float] | None:
                cov = m.cover.overlap(a, b)
                if cov < 60:
                    return None
                v = sum(p.home_cov.overlap(a, b) for p in persons) / cov
                return (v, v, v)
            name, unit = "People at home", "people"
        out["subject"] = {"key": subject, "kind": "count", "name": name, "unit": unit}
        out.update(_count_patterns(m, days, cell))

        def daily(a: int, b: int) -> tuple[float | None, float | None]:
            got = cell(a, b)
            return (got[0], got[2]) if got else (None, None)

        out["calendar"] = _calendar(m, daily)
    else:
        kind, name, raw, home, home_cov, members, tracking = _subject_tracks(m, subject)
        counted = {e.key for e in tracking}
        out["subject"] = {
            "key": subject, "kind": kind, "name": name, "unit": "share",
            "members": [
                {**_entity_brief(e), "counted": e.key in counted}
                for e in sorted(members, key=lambda e: (e.key not in counted, -(e.last_on or 0)))
            ],
            **_status(m, raw, home),
        }
        out.update(_presence_patterns(m, days, raw, home, home_cov))

        def share(a: int, b: int) -> tuple[float | None, float | None]:
            cov = m.cover.overlap(a, b)
            here = home_cov.overlap(a, b)
            return (here / cov if cov >= 3600 else None, here / 3600)

        out["calendar"] = _calendar(m, share)
    return out


def _calendar(
    m: Model, measure: Callable[[int, int], tuple[float | None, float | None]]
) -> list[list[Any]]:
    """One value per local day over the whole history: ``[date, value, extra, monitored_h]``.

    For a person or device ``value`` is the share of monitored time at home and
    ``extra`` the hours; for a count it is the day's mean and ``extra`` its peak.
    """
    out = []
    for day in days_between(m.first_day, m.today):
        a = day_start(m.tz, day)
        b = min(day_start(m.tz, day + timedelta(days=1)), m.now)
        cov = m.cover.overlap(a, b)
        value, extra = measure(a, b) if cov else (None, None)
        out.append([
            day.isoformat(),
            round(value, 3) if value is not None else None,
            round(extra, 1) if extra is not None else None,
            round(cov / 3600, 1),
        ])
    return out


def _week_matrix(acc: list[list[list[float]]], digits: int) -> dict[str, Any]:
    """7 x 24 averages from per-cell [numerator, denominator] sums."""
    values = [
        [round(num / den, digits) if den > 0 else None for num, den in row] for row in acc
    ]
    flat = [v for row in values for v in row if v is not None]
    return {"values": values, "max": max(flat) if flat else 0, "min": min(flat) if flat else 0}


def _presence_patterns(
    m: Model, days: list[date], raw: Track, home: Track, home_cov: Track
) -> dict[str, Any]:
    tz, now = m.tz, m.now
    week = [[[0.0, 0.0] for _ in range(24)] for _ in range(7)]  # [present_s, monitored_s]
    profile = {"weekday": [[0.0, 0.0] for _ in range(48)], "weekend": [[0.0, 0.0] for _ in range(48)]}
    rows = []
    for day in days:
        edges = half_hour_edges(tz, day)
        a, b = edges[0], min(edges[-1], now)
        dow = day.weekday()
        part = profile["weekend" if dow >= 5 else "weekday"]
        present = monitored = 0
        for i in range(48):
            lo, hi = edges[i], min(edges[i + 1], now)
            if hi <= lo:
                continue
            cov = m.cover.overlap(lo, hi)
            if not cov:
                continue
            pres = home_cov.overlap(lo, hi)
            week[dow][i // 2][0] += pres
            week[dow][i // 2][1] += cov
            part[i][0] += pres
            part[i][1] += cov
            present += pres
            monitored += cov
        rows.append({
            "date": day.isoformat(),
            "dow": dow,
            "covered_h": round(monitored / 3600, 2),
            "value": round(present / 3600, 2),
            "on": day_spans(raw.slice(a, b), tz, day),
            "home": day_spans(home.slice(a, b), tz, day),
            "gaps": day_spans(
                [g for g in complement(m.cover.slice(a, b), a, b) if g[1] - g[0] >= m.sample_gap], tz, day
            ),
        })

    w0, w1 = day_start(tz, days[0]), now
    total_cov = m.cover.overlap(w0, w1)
    away = intersect(complement(home.slice(w0, w1), w0, w1), m.cover.slice(w0, w1))
    longest = max(away, key=lambda iv: iv[1] - iv[0], default=None)
    full = [r for r in rows if r["covered_h"] >= 20]

    def mean_hours(pick: Callable[[dict[str, Any]], bool]) -> float | None:
        vals = [r["value"] * 24 / r["covered_h"] for r in full if pick(r)]
        return round(sum(vals) / len(vals), 1) if vals else None

    def usual(cells: list[list[float]]) -> dict[str, Any] | None:
        """The usual shape of a day, from how often each half hour was spent at home:
        mostly home, mostly away, or the stretch that is the exception either way."""
        shares = [p / c if c >= 3600 else None for p, c in cells]
        if all(s is None for s in shares):
            return None
        run = longest_low_run(shares)  # the longest stretch that is usually spent away
        if run is None or run[1] < 4:  # under two hours: no real pattern of absence
            return {"kind": "home"}
        if run[1] >= 44:
            return {"kind": "away"}
        start, end = run[0] * 30, (run[0] + run[1]) % 48 * 30
        if run[1] <= 24:
            return {"kind": "out", "from_min": start, "to_min": end}
        return {"kind": "in", "from_min": end, "to_min": start}  # home is the shorter part

    return {
        "week": _week_matrix(week, 3),
        "days": rows[::-1],
        "stats": {
            "share": round(home_cov.overlap(w0, w1) / total_cov, 3) if total_cov else None,
            "connected_share": round(raw.overlap(w0, w1) / total_cov, 3) if total_cov else None,
            "weekday_h": mean_hours(lambda r: r["dow"] < 5),
            "weekend_h": mean_hours(lambda r: r["dow"] >= 5),
            "usual_weekday": usual(profile["weekday"]),
            "usual_weekend": usual(profile["weekend"]),
            "longest_away": {"start": longest[0], "end": longest[1]} if longest else None,
            "days_seen": sum(1 for r in rows if r["value"] > 0),
            "days_away": sum(1 for r in rows if r["covered_h"] >= 12 and r["value"] < 0.25),
            "days_monitored": sum(1 for r in rows if r["covered_h"] >= 12),
        },
    }


def _count_patterns(
    m: Model, days: list[date], cell: Callable[[int, int], tuple[float, float, float] | None]
) -> dict[str, Any]:
    tz, now = m.tz, m.now
    week = [[[0.0, 0.0] for _ in range(24)] for _ in range(7)]  # [sum of means, cells]
    rows = []
    peak: tuple[float, int] | None = None
    total = cells_n = 0.0
    for day in days:
        edges = half_hour_edges(tz, day)
        dow = day.weekday()
        values: list[float | None] = []
        day_sum = day_n = 0.0
        day_low = day_peak = None
        for i in range(48):
            lo, hi = edges[i], min(edges[i + 1], now)
            got = cell(lo, hi) if hi > lo else None
            if got is None:
                values.append(None)
                continue
            mean, low, high = got
            values.append(round(mean, 2))
            week[dow][i // 2][0] += mean
            week[dow][i // 2][1] += 1
            day_sum += mean
            day_n += 1
            day_low = low if day_low is None else min(day_low, low)
            day_peak = high if day_peak is None else max(day_peak, high)
            if peak is None or high > peak[0]:
                peak = (high, lo)
        total += day_sum
        cells_n += day_n
        rows.append({
            "date": day.isoformat(),
            "dow": dow,
            "covered_h": round(day_n / 2, 1),
            "value": round(day_sum / day_n, 2) if day_n else None,
            "low": round(day_low, 1) if day_low is not None else None,
            "peak": round(day_peak, 1) if day_peak is not None else None,
            "cells": values,
        })
    matrix = _week_matrix(week, 2)
    flat = [(v, d, h) for d, row in enumerate(matrix["values"]) for h, v in enumerate(row) if v is not None]
    busiest = max(flat, default=None)
    quietest = min(flat, default=None)
    return {
        "week": matrix,
        "days": rows[::-1],
        "stats": {
            "mean": round(total / cells_n, 1) if cells_n else None,
            "busiest": {"value": busiest[0], "dow": busiest[1], "hour": busiest[2]} if busiest else None,
            "quietest": {"value": quietest[0], "dow": quietest[1], "hour": quietest[2]} if quietest else None,
            "peak": {"value": round(peak[0], 1), "t": peak[1]} if peak else None,
            "always_on": sum(1 for e in m.entities.values() if e.kind == "always_on"),
            "days_monitored": sum(1 for r in rows if r["covered_h"] >= 12),
        },
    }


# -- devices ---------------------------------------------------------------------


def devices(m: Model) -> dict[str, Any]:
    """Every device, with the MAC addresses behind it, for the Devices table."""
    ents = []
    for e in m.entities.values():
        ents.append({
            "key": f"dev:{e.key}",
            "name": e.name,
            "hostname": e.hostname,
            "label": e.label,
            "owner": e.owner,
            "suggestion": e.suggestion,
            "kind": e.kind,
            "active": e.active,
            "hidden": e.hidden,
            "watched": e.watched,
            "carried": e.carried,
            "carried_auto": e.carried_set is None,
            "merged": e.key.startswith("name:"),
            "first_seen": min(d["first_seen"] for d in e.rows),
            "last_seen": e.last_on if e.raw else None,
            "first_on": e.first_on,
            "uptime": round(e.uptime, 3) if e.uptime is not None else None,
            "ip": e.rows[0]["last_ip"],
            "interface": e.rows[0]["interface_type"],
            "randomized": any(is_randomized_mac(d["mac"]) for d in e.rows),
            "macs": [
                {
                    "mac": d["mac"],
                    "ip": d["last_ip"],
                    "interface": d["interface_type"],
                    "first_seen": d["first_seen"],
                    "last_seen": d["last_seen"],
                    "active": d["on_now"],
                    "watched": bool(d["watched"]),
                    "hidden": bool(d["hidden"]),
                    "randomized": is_randomized_mac(d["mac"]),
                }
                for d in e.rows
            ],
        })
    ents.sort(key=lambda r: (not r["active"], -(r["last_seen"] or 0), r["name"].lower()))
    suggestions: dict[str, list[str]] = defaultdict(list)
    for e in m.entities.values():
        if e.suggestion:
            suggestions[e.suggestion].extend(d["mac"] for d in e.rows)
    return {
        "now": m.now,
        "devices": ents,
        "owners": sorted(m.people, key=str.lower),
        "suggestions": [
            {"owner": o, "macs": macs, "devices": sum(1 for e in m.entities.values() if e.suggestion == o)}
            for o, macs in sorted(suggestions.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        ],
        "summary": {
            "devices": sum(1 for e in m.entities.values() if e.raw),
            "addresses": sum(len(e.rows) for e in m.entities.values()),
            "never": sum(1 for e in m.entities.values() if not e.raw),
            "online": sum(1 for e in m.entities.values() if e.active),
            "since": m.data_start or None,
        },
    }


def device_days(m: Model, span_days: int) -> dict[str, Any]:
    """Hours at home per device per day (or per week over long spans): the matrix
    that shows when each device first appeared, how long it stayed, and its gaps."""
    last = m.today
    first = m.first_day if not span_days else max(m.first_day, last - timedelta(days=span_days - 1))
    days = days_between(first, last)
    step = 1 if len(days) <= 120 else 7
    bins = []
    for i in range(0, len(days), step):
        chunk = days[i: i + step]
        a = day_start(m.tz, chunk[0])
        b = min(day_start(m.tz, chunk[-1] + timedelta(days=1)), m.now)
        bins.append((chunk[0], len(chunk), a, b, m.cover.overlap(a, b)))
    rows = []
    for e in m.entities.values():
        if not e.raw or e.home_cov.overlap(bins[0][2], bins[-1][3]) <= 0:
            continue
        cells = []
        for i, (_, n, a, b, cov) in enumerate(bins):
            sec = e.home_cov.overlap(a, b)
            if sec > 0:
                # Hours per fully monitored day, so partly monitored days compare fairly.
                cells.append([i, round(min(24.0, sec / cov * 24), 1)])
        rows.append({**_entity_brief(e), "hidden": e.hidden, "first_on": e.first_on, "cells": cells})
    rows.sort(key=lambda r: (-r["first_on"], r["name"].lower()))
    return {
        "now": m.now,
        "step_days": step,
        "bins": [{"date": d.isoformat(), "days": n, "covered_h": round(cov / 3600, 1)} for d, n, _, _, cov in bins],
        "devices": rows,
    }


# -- health ----------------------------------------------------------------------


def health(m: Model, span_days: int) -> dict[str, Any]:
    """Is the gear behaving: monitoring gaps, always-on devices that dropped out,
    and devices that keep falling off Wi-Fi."""
    t1 = m.now
    t0 = max(t1 - span_days * 86400, m.data_start or t1)
    span = max(t1 - t0, 1)
    covered = m.cover.overlap(t0, t1)
    gaps = [g for g in complement(m.cover.slice(t0, t1), t0, t1) if g[1] - g[0] >= m.sample_gap]

    always = []
    for e in m.entities.values():
        if e.kind != "always_on":
            continue
        a = max(t0, e.first_on or t0)
        monitored = m.cover.overlap(a, t1)
        down = intersect(complement(e.raw.slice(a, t1), a, t1), m.cover.slice(a, t1))
        outages = [d for d in down if d[1] - d[0] >= SHORT_DROP_S]
        always.append({
            **_entity_brief(e),
            "uptime": round(e.raw.overlap(a, t1) / monitored, 4) if monitored else None,
            "outages": len(outages),
            "longest_s": max((d[1] - d[0] for d in outages), default=0),
            "down_s": sum(d[1] - d[0] for d in outages),
            "down": _lists(d for d in down if d[1] - d[0] >= 60),
            "from": a,
        })
    always.sort(key=lambda r: (r["active"], -(r["down_s"]), r["name"].lower()))

    covered_days = max(covered / 86400, 1 / 24)
    drops = []
    for e in m.entities.values():
        if not e.raw:
            continue
        n = short_drops(e.raw, m.cover, t0, t1)
        if n < 3:
            continue
        connected = e.raw.overlap(t0, t1)
        drops.append({
            **_entity_brief(e),
            "drops": n,
            "per_day": round(n / covered_days, 1),
            "connected_h_per_day": round(connected / 3600 / covered_days, 1),
            "per_connected_h": round(n / (connected / 3600), 2) if connected else None,
        })
    drops.sort(key=lambda r: -r["drops"])

    return {
        "now": t1,
        "start": t0,
        "end": t1,
        "days": span_days,
        "stale": m.stale,
        "last_sample": m.last_sample or None,
        "coverage": {
            "share": round(covered / span, 4),
            "spans": _lists(m.cover.slice(t0, t1)),
            "gaps": _lists(gaps),
            "longest_s": max((g[1] - g[0] for g in gaps), default=0),
        },
        "always_on": always,
        "drops": drops[:12],
        "drops_total": sum(d["drops"] for d in drops),
        "drops_per_day": round(sum(d["drops"] for d in drops) / covered_days, 1),
    }
