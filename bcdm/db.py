"""SQLite persistence for BellDeviceMonitor.

One long-lived connection in WAL mode, serialized by a lock. Public methods are
async and run the synchronous SQLite work in a worker thread so the event loop
is never blocked. The hot path is :meth:`Database.apply_poll`, which diffs a poll
against stored state in a single transaction. The dashboard reads through
:meth:`Database.insight`, which hands a consistent snapshot of the history to one
of the payload builders in :mod:`bcdm.insights`.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from .analytics import BUCKET_S, SAMPLE_GAP_S
from .insights import Model, State, build_model, find_devices, subject_addresses
from .util import (
    TS_FORMAT,
    iso_to_epoch,
    parse_ts,
    since_str,
    to_iso_z,
    utc_now_str,
    x_time_ago,
)

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
    hidden         INTEGER NOT NULL DEFAULT 0,
    owner          TEXT,
    carried        INTEGER
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
"""

# Columns added after the first release: (table, column, declaration).
# owner:   the person a device belongs to.
# carried: whether it goes where its owner goes (1/0), or NULL to judge by its hostname.
_ADDED_COLUMNS = (("devices", "owner", "TEXT"), ("devices", "carried", "INTEGER"))

# Databases from before November 2026 also hold this table: one row per poll and
# connection type (Ethernet / WiFi), which fed a chart that no longer exists. Nothing
# reads or writes it now, and the same numbers follow from each device's connection
# type and its presence history. `python -m bcdm --drop-interface-samples` removes it.
LEGACY_TABLE = "interface_samples"

#: A device whose record did not change still gets its last_seen brought up to date
#: this often, so last_seen keeps meaning "the router listed it about then".
LAST_SEEN_REFRESH_S = 3600

_EPOCH = "CAST(strftime('%s', {}) AS INTEGER)"
_EDITABLE = ("label", "owner", "watched", "hidden", "carried")
_UTC = ZoneInfo("UTC")
OWNER_MAX_LEN = 40


def clean_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Keep only the editable device fields, normalized for storage."""
    out: dict[str, Any] = {}
    for key in _EDITABLE:
        if key not in fields:
            continue
        value = fields[key]
        if key in ("watched", "hidden"):
            if value is not None:
                out[key] = int(bool(value))
        elif key == "carried":
            out[key] = None if value is None else int(bool(value))
        else:
            text = (value or "").strip()
            out[key] = (text[:OWNER_MAX_LEN] if key == "owner" else text) or None
    return out


class Database:
    def __init__(self, path: str | Path, sample_gap_s: int = SAMPLE_GAP_S):
        self._path = str(path)
        self._sample_gap = int(sample_gap_s)
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        # Derived from the history and kept up to date incrementally (see _state).
        self._cover: list[list[int]] | None = None
        self._cover_last: str | None = None
        self._rollup: dict[int, tuple[int, int, int, int]] | None = None
        self._rollup_last = 0
        self._events: dict[str, list[tuple[int, bool]]] | None = None
        self._events_ver = 0
        self._memo: dict[Any, Any] = {}
        self._data_version: int | None = None
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(SCHEMA)
            for table, column, decl in _ADDED_COLUMNS:
                have = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
                if column not in have:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            self._conn.commit()

    # -- async wrappers ------------------------------------------------------

    async def apply_poll(
        self, hosts: list[dict[str, Any]], new_device_window_min: int = 10
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        return await self._run(self._apply_poll, hosts, new_device_window_min)

    async def insight(
        self, build: Callable[..., dict[str, Any]], tz: ZoneInfo, now: int, *args: Any
    ) -> dict[str, Any]:
        """Run a payload builder from :mod:`bcdm.insights` on the current history."""
        return await self._run(self._insight, build, tz, now, args)

    async def get_device(self, mac: str) -> dict[str, Any] | None:
        return await self._run(self._get_device, mac)

    async def get_device_events(self, mac: str, limit: int = 100) -> list[dict[str, Any]]:
        return await self._run(self._get_device_events, mac, limit)

    async def update_device(self, mac: str, **fields: Any) -> dict[str, Any] | None:
        return await self._run(self._update_device, mac, fields)

    async def update_devices(self, macs: list[str], **fields: Any) -> int:
        """Apply the same edit to several MAC addresses; returns how many changed."""
        return await self._run(self._update_devices, macs, fields)

    async def prune(self, retention_days: int) -> int:
        return await self._run(self._prune, retention_days)

    async def find_subjects(self, kind: str, text: str, now: int) -> list[dict[str, Any]]:
        """What "forget this owner / this device" would remove, without removing it.

        ``kind`` is "owner" or "device"; a device is named by a MAC address or by its
        name. Each match is ``{subject, name, addresses: [{mac, name, events}]}``.
        """
        return await self._run(self._find_subjects, kind, text, now)

    async def forget_subject(self, subject: str, now: int) -> dict[str, Any]:
        """Delete a person's devices ("owner:<name>") or one device ("dev:<key>"), with
        every MAC address behind them and all of their presence history."""
        return await self._run(self._forget_subject, subject, now)

    async def drop_legacy_table(self, *, dry_run: bool = False) -> dict[str, Any]:
        """Remove the unused connection-type log left in older databases."""
        return await self._run(self._drop_legacy_table, dry_run)

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
        # A device's row is written only when something about it changed, or to bring
        # last_seen up to date about once an hour. Rewriting every listed device on
        # every poll was most of what the monitor wrote to disk, which matters on an
        # SD card.
        refresh_before = (parse_ts(ts) - timedelta(seconds=LAST_SEEN_REFRESH_S)).strftime(TS_FORMAT)
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
                changed = (row["name"], row["last_ip"], row["interface_type"], was_active) != (
                    h["name"], h["ip"], h["interface_type"], active
                )
                if changed or row["last_seen"] <= refresh_before:
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
                    "active_last_change_ts": iso_to_epoch(h.get("active_last_change")),
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

    # -- history snapshot for the dashboard ----------------------------------

    def _insight(
        self, build: Callable[..., dict[str, Any]], tz: ZoneInfo, now: int, args: tuple[Any, ...]
    ) -> dict[str, Any]:
        model: Model = build_model(self._state(now), tz)
        return build(model, *args)

    def _forget_derived(self) -> None:
        """Drop everything worked out from the stored history; it is rebuilt on demand."""
        self._cover = self._rollup = self._events = None
        self._cover_last, self._rollup_last, self._events_ver = None, 0, 0
        self._memo.clear()

    def _state(self, now: int) -> State:
        # data_version moves only when *another* connection commits: a forget run from
        # the command line, a restored backup. What was derived may then be out of date.
        version = self._conn.execute("PRAGMA data_version").fetchone()[0]
        if version != self._data_version:
            if self._data_version is not None:
                self._forget_derived()
            self._data_version = version
        return State(
            now=now,
            sample_gap=self._sample_gap,
            cover=self._coverage(),
            devices=self._device_rows(),
            events=self._presence_events(),
            rollup=self._counts(),
            version=self._events_ver,
            memo=self._memo,
        )

    def _coverage(self) -> list[tuple[int, int]]:
        """Spans in which polls kept succeeding (no gap longer than the sample gap).

        Built once from every sample, then extended with the samples that arrived
        since the previous call. A poll at second ``t`` covers ``[t, t + 1)``: the
        state at the latest poll is known, so a device that came online in that very
        poll counts as connected straight away, not one poll later.
        """
        gap = self._sample_gap
        if self._cover is None:
            spans: list[list[int]] = []
            inner = (
                f"SELECT {_EPOCH.format('ts')} AS t,"
                f" {_EPOCH.format('LAG(ts) OVER (ORDER BY ts)')} AS prev FROM count_samples"
            )
            for t, prev in self._conn.execute(
                f"SELECT t, prev FROM ({inner}) WHERE prev IS NULL OR t - prev > ? ORDER BY t", (gap,)
            ):
                if spans:
                    spans[-1][1] = prev
                spans.append([t, t])
            last_ts, last = self._conn.execute(
                f"SELECT MAX(ts), {_EPOCH.format('MAX(ts)')} FROM count_samples"
            ).fetchone()
            if spans:
                spans[-1][1] = last
            self._cover, self._cover_last = spans, last_ts
        else:
            spans = self._cover
            sql = f"SELECT ts, {_EPOCH.format('ts')} FROM count_samples"
            args: tuple[Any, ...] = ()
            if self._cover_last:
                sql, args = sql + " WHERE ts > ?", (self._cover_last,)
            for ts, t in self._conn.execute(sql + " ORDER BY ts", args):
                if not spans or t - spans[-1][1] > gap:
                    spans.append([t, t])
                else:
                    spans[-1][1] = t
                self._cover_last = ts
        return [(s, e + 1) for s, e in spans]

    def _counts(self) -> dict[int, tuple[int, int, int, int]]:
        """Connected-device count per 15 minutes: (samples, sum, min, max)."""
        sql = (
            f"SELECT {_EPOCH.format('ts')} / {BUCKET_S} AS b, COUNT(*), SUM(active_count),"
            " MIN(active_count), MAX(active_count) FROM count_samples{} GROUP BY b"
        )
        if self._rollup is None:
            self._rollup = {}
            rows = self._conn.execute(sql.format(""))
        else:
            # Re-read from the newest bucket on: it may have gained samples.
            since = datetime.fromtimestamp(self._rollup_last * BUCKET_S, timezone.utc)
            rows = self._conn.execute(sql.format(" WHERE ts >= ?"), (since.strftime(TS_FORMAT),))
        for b, n, total, lo, hi in rows:
            self._rollup[b] = (n, total, lo, hi)
            if b > self._rollup_last:
                self._rollup_last = b
        return self._rollup

    def _presence_events(self) -> dict[str, list[tuple[int, bool]]]:
        """Every online/offline transition per MAC, appended to as events arrive."""
        ver = self._conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM presence_events").fetchone()[0]
        if self._events is None or ver < self._events_ver:
            self._events, self._events_ver = {}, 0
        if ver != self._events_ver:
            for t, mac, event in self._conn.execute(
                f"SELECT {_EPOCH.format('ts')}, mac, event FROM presence_events"
                " WHERE rowid > ? ORDER BY ts, rowid",
                (self._events_ver,),
            ):
                self._events.setdefault(mac, []).append((t, event in ("online", "new")))
            self._events_ver = ver
        return self._events

    def _device_rows(self) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self._conn.execute(
                "SELECT mac, name, label, owner, carried, last_ip, interface_type, is_active, watched, hidden,"
                f" {_EPOCH.format('first_seen')} AS first_seen, {_EPOCH.format('last_seen')} AS last_seen"
                " FROM devices"
            )
        ]

    # -- single devices --------------------------------------------------------

    def _get_device(self, mac: str) -> dict[str, Any] | None:
        r = self._conn.execute("SELECT * FROM devices WHERE mac = ?", (mac,)).fetchone()
        return self._device_dict(r) if r else None

    def _get_device_events(self, mac: str, limit: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT ts, event FROM presence_events WHERE mac = ? ORDER BY ts DESC LIMIT ?",
            (mac, limit),
        ).fetchall()
        return [{"ts": to_iso_z(r["ts"]), "event": r["event"]} for r in rows]

    def _update_device(self, mac: str, fields: dict[str, Any]) -> dict[str, Any] | None:
        self._update_devices([mac], fields)
        return self._get_device(mac)

    def _update_devices(self, macs: list[str], fields: dict[str, Any]) -> int:
        sets = clean_fields(fields)
        if not sets or not macs:
            return 0
        cols = ", ".join(f"{k} = ?" for k in sets)
        marks = ",".join("?" * len(macs))
        changed = self._conn.execute(
            f"UPDATE devices SET {cols} WHERE mac IN ({marks})", (*sets.values(), *macs)
        ).rowcount
        self._conn.commit()
        return changed

    def _prune(self, retention_days: int) -> int:
        cutoff = since_str(days=retention_days)
        tables = ["count_samples"] + ([LEGACY_TABLE] if self._has_table(LEGACY_TABLE) else [])
        deleted = 0
        for table in tables:
            deleted += self._conn.execute(
                f"DELETE FROM {table} WHERE ts < ?", (cutoff,)
            ).rowcount
        self._conn.commit()
        if deleted:  # the derived history no longer matches what is stored
            self._cover = self._rollup = None
            self._cover_last, self._rollup_last = None, 0
        return deleted

    def _has_table(self, name: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone() is not None

    def _drop_legacy_table(self, dry_run: bool) -> dict[str, Any]:
        if not self._has_table(LEGACY_TABLE):
            return {"present": False, "rows": 0, "bytes": 0}
        rows = self._conn.execute(f"SELECT COUNT(*) FROM {LEGACY_TABLE}").fetchone()[0]
        if dry_run:
            return {"present": True, "rows": rows, "bytes": 0}
        page = self._conn.execute("PRAGMA page_size").fetchone()[0]
        free = self._conn.execute("PRAGMA freelist_count").fetchone()[0]
        self._conn.execute(f"DROP TABLE {LEGACY_TABLE}")  # takes its index with it
        self._conn.commit()
        freed = self._conn.execute("PRAGMA freelist_count").fetchone()[0] - free
        return {"present": True, "rows": rows, "bytes": freed * page}

    # -- forgetting ----------------------------------------------------------

    def _describe(self, macs: list[str]) -> list[dict[str, Any]]:
        out = []
        for mac in macs:
            row = self._conn.execute(
                "SELECT d.mac, COALESCE(d.label, d.name) AS name,"
                " (SELECT COUNT(*) FROM presence_events e WHERE e.mac = d.mac) AS events"
                " FROM devices d WHERE d.mac = ?",
                (mac,),
            ).fetchone()
            if row:
                out.append(dict(row))
        return out

    def _find_subjects(self, kind: str, text: str, now: int) -> list[dict[str, Any]]:
        model = build_model(self._state(now), _UTC)
        if kind == "owner":
            wanted = text.strip().lower()
            keys = [f"owner:{name}" for name in model.people if name.lower() == wanted]
        else:
            keys = [f"dev:{e.key}" for e in find_devices(model, text)]
        out = []
        for key in keys:
            name, macs = subject_addresses(model, key)
            out.append({"subject": key, "name": name, "addresses": self._describe(macs)})
        return out

    def _forget_subject(self, subject: str, now: int) -> dict[str, Any]:
        name, macs = subject_addresses(build_model(self._state(now), _UTC), subject)
        return {"name": name, **self._forget(macs)}

    def _forget(self, macs: list[str]) -> dict[str, int]:
        """Delete devices and their history: events first, since they refer to the device."""
        macs = sorted({m.upper() for m in macs})
        events = addresses = 0
        for i in range(0, len(macs), 500):
            chunk = macs[i: i + 500]
            marks = ",".join("?" * len(chunk))
            events += self._conn.execute(
                f"DELETE FROM presence_events WHERE mac IN ({marks})", chunk
            ).rowcount
            addresses += self._conn.execute(
                f"DELETE FROM devices WHERE mac IN ({marks})", chunk
            ).rowcount
        self._conn.commit()
        # The history kept in memory still has them; were an address to come back,
        # it must start from nothing.
        self._events, self._events_ver = None, 0
        self._memo.clear()
        return {"addresses": addresses, "events": events}

    @staticmethod
    def _device_dict(r: sqlite3.Row) -> dict[str, Any]:
        return {
            "mac": r["mac"],
            "name": r["label"] or r["name"] or r["mac"],
            "router_name": r["name"],
            "label": r["label"],
            "owner": r["owner"],
            "carried": None if r["carried"] is None else bool(r["carried"]),
            "last_ip": r["last_ip"],
            "interface_type": r["interface_type"],
            "first_seen": to_iso_z(r["first_seen"]),
            "last_seen": to_iso_z(r["last_seen"]),
            "is_active": bool(r["is_active"]),
            "watched": bool(r["watched"]),
            "hidden": bool(r["hidden"]),
        }
