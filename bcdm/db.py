"""SQLite persistence for BellDeviceMonitor.

One long-lived connection in WAL mode, serialized by a lock. Public methods are
async and run the synchronous SQLite work in a worker thread so the event loop
is never blocked. The hot path is :meth:`Database.apply_poll`, which diffs a poll
against stored state in a single transaction.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .util import TS_FORMAT, parse_ts, since_str, to_iso_z, utc_now, utc_now_str, x_time_ago

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    mac            TEXT PRIMARY KEY,
    name           TEXT,
    label          TEXT,
    last_ip        TEXT,
    interface_type TEXT,
    first_seen     TEXT NOT NULL,
    last_seen      TEXT NOT NULL,
    is_active      INTEGER NOT NULL DEFAULT 0,
    watched        INTEGER NOT NULL DEFAULT 0,
    hidden         INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS count_samples (
    ts           TEXT NOT NULL,
    active_count INTEGER NOT NULL,
    total_count  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_count_samples_ts ON count_samples(ts);

CREATE TABLE IF NOT EXISTS presence_events (
    ts    TEXT NOT NULL,
    mac   TEXT NOT NULL,
    event TEXT NOT NULL,
    FOREIGN KEY (mac) REFERENCES devices(mac)
);
CREATE INDEX IF NOT EXISTS idx_presence_events_mac_ts ON presence_events(mac, ts);

CREATE TABLE IF NOT EXISTS interface_samples (
    ts             TEXT NOT NULL,
    interface_type TEXT NOT NULL,
    active_count   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_interface_samples_ts ON interface_samples(ts);
"""

# range key -> (timedelta kwargs for the lookback window, strftime bucket or None for raw)
_HISTORY_RANGES: dict[str, tuple[dict[str, float], str | None]] = {
    "1h": ({"hours": 1}, None),
    "24h": ({"hours": 24}, "%Y-%m-%d %H:%M"),
    "30d": ({"days": 30}, "%Y-%m-%d %H"),
    "1y": ({"days": 365}, "%Y-%m-%d"),
}


def _intervals_from_events(
    events: list[sqlite3.Row], start: str, end: str, is_active_now: bool
) -> list[tuple[str, str]]:
    """Reconstruct online intervals within [start, end] from presence events.

    Determines the state at `start` from the last event before it; failing that,
    infers it from the first in-range event; failing that (no events at all, e.g.
    a baseline-seeded device), assumes the steady current state. This makes a
    device that's been online since first boot show as online for the whole range.
    """
    transitions = [(e["ts"], e["event"] in ("online", "new")) for e in events]
    before = [t for t in transitions if t[0] < start]
    in_range = [t for t in transitions if start <= t[0] <= end]
    if before:
        online = before[-1][1]
    elif in_range:
        online = not in_range[0][1]
    else:
        online = is_active_now

    intervals: list[tuple[str, str]] = []
    seg_start = start if online else None
    for ts, to_online in in_range:
        if to_online and not online:
            online, seg_start = True, ts
        elif not to_online and online:
            online = False
            intervals.append((seg_start, ts))  # type: ignore[arg-type]
            seg_start = None
    if online and seg_start is not None:
        intervals.append((seg_start, end))
    return [(s, e) for s, e in intervals if s < e]  # drop zero-width spans


def _overlap_seconds(s: datetime, e: datetime, a: datetime, b: datetime) -> float:
    """Seconds that interval [s, e] overlaps window [a, b]."""
    lo, hi = max(s, a), min(e, b)
    return max(0.0, (hi - lo).total_seconds())


class Database:
    def __init__(self, path: str | Path):
        self._path = str(path)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # -- async wrappers ------------------------------------------------------

    async def apply_poll(
        self, hosts: list[dict[str, Any]], new_device_window_min: int = 10
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return await self._run(self._apply_poll, hosts, new_device_window_min)

    async def get_count_history(self, range_key: str) -> list[dict[str, Any]]:
        return await self._run(self._get_count_history, range_key)

    async def list_devices(self) -> list[dict[str, Any]]:
        return await self._run(self._list_devices)

    async def get_device(self, mac: str) -> dict[str, Any] | None:
        return await self._run(self._get_device, mac)

    async def get_device_events(self, mac: str, limit: int = 100) -> list[dict[str, Any]]:
        return await self._run(self._get_device_events, mac, limit)

    async def get_recent_events(self, limit: int = 25) -> list[dict[str, Any]]:
        return await self._run(self._get_recent_events, limit)

    async def get_presence_timeline(
        self, range_key: str, scope: str = "watched", limit: int = 40
    ) -> dict[str, Any]:
        return await self._run(self._get_presence_timeline, range_key, scope, limit)

    async def get_presence_heatmap(
        self, days: int, scope: str = "all", limit: int = 40
    ) -> dict[str, Any]:
        return await self._run(self._get_presence_heatmap, days, scope, limit)

    async def get_daily_activity(self, days: int) -> dict[str, Any]:
        return await self._run(self._get_daily_activity, days)

    async def get_interface_history(self, range_key: str) -> dict[str, Any]:
        return await self._run(self._get_interface_history, range_key)

    async def update_device(self, mac: str, **fields: Any) -> dict[str, Any] | None:
        return await self._run(self._update_device, mac, fields)

    async def prune(self, retention_days: int) -> int:
        return await self._run(self._prune, retention_days)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    async def _run(self, fn: Callable[..., Any], *args: Any) -> Any:
        def locked() -> Any:
            with self._lock:
                return fn(*args)

        return await asyncio.to_thread(locked)

    # -- synchronous impls (lock held by _run) -------------------------------

    def _apply_poll(
        self, hosts: list[dict[str, Any]], window_min: int
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        ts = utc_now_str()
        cur = self._conn.cursor()
        existing = {r["mac"]: r for r in cur.execute("SELECT * FROM devices")}
        is_initial = not existing  # first ever poll: seed baseline, don't alert

        hosts = [h for h in hosts if h.get("mac")]
        active_count = sum(1 for h in hosts if h["active"])
        total_count = len(hosts)
        seen: set[str] = set()
        events: list[dict[str, Any]] = []

        def evt(mac: str, kind: str, row: sqlite3.Row | None, host: dict | None) -> None:
            name = (row["name"] if row else None) or (host["name"] if host else None)
            ip = (host["ip"] if host else None) or (row["last_ip"] if row else None)
            watched = bool(row["watched"]) if row else False
            events.append({"mac": mac, "event": kind, "name": name, "ip": ip, "watched": watched})

        for h in hosts:
            mac = h["mac"]
            seen.add(mac)
            row = existing.get(mac)
            active = bool(h["active"])
            if row is None:
                cur.execute(
                    "INSERT INTO devices (mac, name, last_ip, interface_type, first_seen,"
                    " last_seen, is_active) VALUES (?,?,?,?,?,?,?)",
                    (mac, h["name"], h["ip"], h["interface_type"], ts, ts, int(active)),
                )
                if active and not is_initial:
                    evt(mac, "new", None, h)
            else:
                was_active = bool(row["is_active"])
                cur.execute(
                    "UPDATE devices SET name=?, last_ip=?, interface_type=?, last_seen=?,"
                    " is_active=? WHERE mac=?",
                    (h["name"], h["ip"], h["interface_type"], ts, int(active), mac),
                )
                if not is_initial and active and not was_active:
                    evt(mac, "online", row, h)
                elif not is_initial and not active and was_active:
                    evt(mac, "offline", row, h)

        # Known-active devices that vanished from this poll entirely -> offline.
        for mac, row in existing.items():
            if mac not in seen and bool(row["is_active"]):
                cur.execute("UPDATE devices SET is_active=0, last_seen=? WHERE mac=?", (ts, mac))
                if not is_initial:
                    evt(mac, "offline", row, None)

        for e in events:
            cur.execute(
                "INSERT INTO presence_events (ts, mac, event) VALUES (?,?,?)",
                (ts, e["mac"], e["event"]),
            )
        cur.execute(
            "INSERT INTO count_samples (ts, active_count, total_count) VALUES (?,?,?)",
            (ts, active_count, total_count),
        )
        for itype, n in Counter(
            h["interface_type"] or "Unknown" for h in hosts if h["active"]
        ).items():
            cur.execute(
                "INSERT INTO interface_samples (ts, interface_type, active_count) VALUES (?,?,?)",
                (ts, itype, n),
            )
        self._conn.commit()

        now_devices = self._build_now(cur, hosts, window_min)
        return events, now_devices

    def _build_now(
        self, cur: sqlite3.Cursor, hosts: list[dict[str, Any]], window_min: int
    ) -> list[dict[str, Any]]:
        by_mac = {h["mac"]: h for h in hosts if h["active"]}
        if not by_mac:
            return []
        new_threshold = since_str(minutes=window_min)
        # "New" = a new-device event fired recently. Baseline seeding emits no
        # such event, so existing devices aren't falsely flagged on first boot.
        new_macs = {
            r["mac"]
            for r in cur.execute(
                "SELECT DISTINCT mac FROM presence_events WHERE event = 'new' AND ts >= ?",
                (new_threshold,),
            )
        }
        placeholders = ",".join("?" * len(by_mac))
        rows = {
            r["mac"]: r
            for r in cur.execute(
                f"SELECT * FROM devices WHERE mac IN ({placeholders})", tuple(by_mac)
            )
        }
        out: list[dict[str, Any]] = []
        for mac, h in by_mac.items():
            r = rows.get(mac)
            label = r["label"] if r else None
            out.append(
                {
                    "mac": mac,
                    "name": label or h["name"] or mac,
                    "label": label,
                    "router_name": h["name"],
                    "ip": h["ip"],
                    "interface_type": h["interface_type"],
                    "active_last_change": h.get("active_last_change"),
                    "active_since": x_time_ago(h.get("active_last_change")),
                    "first_seen": to_iso_z(r["first_seen"]) if r else None,
                    "last_seen": to_iso_z(r["last_seen"]) if r else None,
                    "watched": bool(r["watched"]) if r else False,
                    "hidden": bool(r["hidden"]) if r else False,
                    "is_new": mac in new_macs,
                }
            )
        out.sort(key=lambda d: d["active_last_change"] or "", reverse=True)
        return out

    def _get_count_history(self, range_key: str) -> list[dict[str, Any]]:
        delta, bucket = _HISTORY_RANGES.get(range_key, _HISTORY_RANGES["24h"])
        since = since_str(**delta)
        if bucket is None:
            rows = self._conn.execute(
                "SELECT ts, active_count AS active, total_count AS total"
                " FROM count_samples WHERE ts >= ? ORDER BY ts",
                (since,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT MIN(ts) AS ts, ROUND(AVG(active_count), 1) AS active,"
                " ROUND(AVG(total_count), 1) AS total FROM count_samples"
                " WHERE ts >= ? GROUP BY strftime(?, ts) ORDER BY ts",
                (since, bucket),
            ).fetchall()
        return [{"ts": to_iso_z(r["ts"]), "active": r["active"], "total": r["total"]} for r in rows]

    def _list_devices(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM devices ORDER BY is_active DESC, last_seen DESC"
        ).fetchall()
        return [self._device_dict(r) for r in rows]

    def _get_device(self, mac: str) -> dict[str, Any] | None:
        r = self._conn.execute("SELECT * FROM devices WHERE mac = ?", (mac,)).fetchone()
        return self._device_dict(r) if r else None

    def _get_device_events(self, mac: str, limit: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT ts, event FROM presence_events WHERE mac = ? ORDER BY ts DESC LIMIT ?",
            (mac, limit),
        ).fetchall()
        return [{"ts": to_iso_z(r["ts"]), "event": r["event"]} for r in rows]

    def _get_recent_events(self, limit: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT e.ts AS ts, e.event AS event, e.mac AS mac,"
            " COALESCE(d.label, d.name, e.mac) AS name"
            " FROM presence_events e LEFT JOIN devices d ON d.mac = e.mac"
            " ORDER BY e.ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {"ts": to_iso_z(r["ts"]), "event": r["event"], "mac": r["mac"], "name": r["name"]}
            for r in rows
        ]

    def _scope_devices(self, scope: str, limit: int) -> list[sqlite3.Row]:
        if scope == "watched":
            rows = self._conn.execute(
                "SELECT * FROM devices WHERE watched = 1 ORDER BY last_seen DESC"
            ).fetchall()
        elif scope == "active":
            rows = self._conn.execute(
                "SELECT * FROM devices WHERE is_active = 1 ORDER BY last_seen DESC"
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM devices ORDER BY is_active DESC, last_seen DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return rows[:limit]

    def _get_presence_timeline(
        self, range_key: str, scope: str, limit: int
    ) -> dict[str, Any]:
        delta, _ = _HISTORY_RANGES.get(range_key, _HISTORY_RANGES["24h"])
        start = since_str(**delta)
        end = utc_now_str()
        rows = self._scope_devices(scope, limit)
        span = max((parse_ts(end) - parse_ts(start)).total_seconds(), 1.0)
        devices: list[dict[str, Any]] = []
        for r in rows:
            events = self._conn.execute(
                "SELECT ts, event FROM presence_events WHERE mac = ? AND ts <= ? ORDER BY ts",
                (r["mac"], end),
            ).fetchall()
            intervals = _intervals_from_events(events, start, end, bool(r["is_active"]))
            online = sum((parse_ts(e) - parse_ts(s)).total_seconds() for s, e in intervals)
            devices.append(
                {
                    "mac": r["mac"],
                    "name": r["label"] or r["name"] or r["mac"],
                    "is_active": bool(r["is_active"]),
                    "watched": bool(r["watched"]),
                    "uptime_pct": round(100 * online / span, 1),
                    "intervals": [{"start": to_iso_z(s), "end": to_iso_z(e)} for s, e in intervals],
                }
            )
        return {
            "range": range_key,
            "scope": scope,
            "start": to_iso_z(start),
            "end": to_iso_z(end),
            "devices": devices,
        }

    def _get_presence_heatmap(self, days: int, scope: str, limit: int) -> dict[str, Any]:
        end_dt = utc_now()
        start_dt = (end_dt - timedelta(days=days - 1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        day_starts = [start_dt + timedelta(days=i) for i in range(days)]
        start = start_dt.strftime(TS_FORMAT)
        end = end_dt.strftime(TS_FORMAT)

        devices = []
        for r in self._scope_devices(scope, limit):
            events = self._conn.execute(
                "SELECT ts, event FROM presence_events WHERE mac = ? AND ts <= ? ORDER BY ts",
                (r["mac"], end),
            ).fetchall()
            intervals = [
                (parse_ts(s), parse_ts(e))
                for s, e in _intervals_from_events(events, start, end, bool(r["is_active"]))
            ]
            cells = []
            for d0 in day_starts:
                d1 = min(d0 + timedelta(days=1), end_dt)
                span = (d1 - d0).total_seconds()
                online = sum(_overlap_seconds(s, e, d0, d1) for s, e in intervals)
                cells.append(round(100 * online / span, 1) if span > 0 else 0.0)
            devices.append(
                {
                    "mac": r["mac"],
                    "name": r["label"] or r["name"] or r["mac"],
                    "is_active": bool(r["is_active"]),
                    "cells": cells,
                }
            )
        return {
            "days": [d.date().isoformat() for d in day_starts],
            "scope": scope,
            "devices": devices,
        }

    def _get_daily_activity(self, days: int) -> dict[str, Any]:
        end_dt = utc_now()
        start_dt = (end_dt - timedelta(days=days - 1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        rows = self._conn.execute(
            "SELECT strftime('%Y-%m-%d', ts) AS day, event, COUNT(*) AS n"
            " FROM presence_events WHERE ts >= ? GROUP BY day, event",
            (start_dt.strftime(TS_FORMAT),),
        ).fetchall()
        agg: dict[str, dict[str, int]] = {}
        for r in rows:
            bucket = agg.setdefault(r["day"], {"new": 0, "online": 0, "offline": 0})
            if r["event"] in bucket:
                bucket[r["event"]] = r["n"]
        day_list = [(start_dt + timedelta(days=i)).date().isoformat() for i in range(days)]
        return {
            "days": day_list,
            "new": [agg.get(d, {}).get("new", 0) for d in day_list],
            "online": [agg.get(d, {}).get("online", 0) for d in day_list],
            "offline": [agg.get(d, {}).get("offline", 0) for d in day_list],
        }

    def _get_interface_history(self, range_key: str) -> dict[str, Any]:
        delta, bucket = _HISTORY_RANGES.get(range_key, _HISTORY_RANGES["24h"])
        since = since_str(**delta)
        if bucket is None:
            rows = self._conn.execute(
                "SELECT ts AS b, ts AS ts, interface_type AS itype, active_count AS c"
                " FROM interface_samples WHERE ts >= ? ORDER BY ts",
                (since,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT strftime(?, ts) AS b, MIN(ts) AS ts, interface_type AS itype,"
                " ROUND(AVG(active_count), 1) AS c FROM interface_samples"
                " WHERE ts >= ? GROUP BY b, interface_type ORDER BY ts",
                (bucket, since),
            ).fetchall()
        buckets: dict[str, dict[str, Any]] = {}
        interfaces: set[str] = set()
        for r in rows:
            itype = r["itype"] or "Unknown"
            interfaces.add(itype)
            entry = buckets.setdefault(r["b"], {"ts": r["ts"], "data": {}})
            entry["data"][itype] = r["c"]
        ordered = sorted(buckets.values(), key=lambda e: e["ts"])
        names = sorted(interfaces)
        return {
            "range": range_key,
            "labels": [to_iso_z(e["ts"]) for e in ordered],
            "series": [
                {"interface": it, "data": [e["data"].get(it, 0) for e in ordered]}
                for it in names
            ],
        }

    def _update_device(self, mac: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        allowed = {"label", "watched", "hidden"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return self._get_device(mac)
        cols = ", ".join(f"{k} = ?" for k in sets)
        self._conn.execute(
            f"UPDATE devices SET {cols} WHERE mac = ?", (*sets.values(), mac)
        )
        self._conn.commit()
        return self._get_device(mac)

    def _prune(self, retention_days: int) -> int:
        cutoff = since_str(days=retention_days)
        deleted = 0
        for table in ("count_samples", "interface_samples"):
            deleted += self._conn.execute(
                f"DELETE FROM {table} WHERE ts < ?", (cutoff,)
            ).rowcount
        self._conn.commit()
        return deleted

    @staticmethod
    def _device_dict(r: sqlite3.Row) -> dict[str, Any]:
        return {
            "mac": r["mac"],
            "name": r["label"] or r["name"] or r["mac"],
            "router_name": r["name"],
            "label": r["label"],
            "last_ip": r["last_ip"],
            "interface_type": r["interface_type"],
            "first_seen": to_iso_z(r["first_seen"]),
            "last_seen": to_iso_z(r["last_seen"]),
            "is_active": bool(r["is_active"]),
            "watched": bool(r["watched"]),
            "hidden": bool(r["hidden"]),
        }
