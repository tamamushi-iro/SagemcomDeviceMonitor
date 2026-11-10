"""What the Database derives from stored history for the dashboard, and device edits.

Run from the project root:  uv run python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from bcdm import insights
from bcdm.__main__ import drop_interface_samples, forget
from bcdm.db import LEGACY_TABLE, Database
from bcdm.util import TS_FORMAT

UTC = ZoneInfo("UTC")
T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


def ts(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).strftime(TS_FORMAT)


def epoch(seconds: float) -> int:
    return int((T0 + timedelta(seconds=seconds)).timestamp())


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "bcdm.db"
        self.db = Database(self.path)
        self.addCleanup(self._dir.cleanup)
        self.addCleanup(self.db.close)

    def add_samples(self, db: Database, start: float, end: float, count: int = 3, step: int = 30) -> None:
        db._conn.executemany(
            "INSERT INTO count_samples (ts, active_count, total_count) VALUES (?,?,?)",
            [(ts(s), count, 10) for s in range(int(start), int(end) + 1, step)],
        )
        db._conn.commit()

    def add_device(self, mac: str, name: str, *, active: bool, events=()) -> None:
        self.db._conn.execute(
            "INSERT INTO devices (mac, name, first_seen, last_seen, is_active) VALUES (?,?,?,?,?)",
            (mac, name, ts(0), ts(0), int(active)),
        )
        self.db._conn.executemany(
            "INSERT INTO presence_events (ts, mac, event) VALUES (?,?,?)",
            [(ts(s), mac, kind) for s, kind in events],
        )
        self.db._conn.commit()

    def test_coverage_splits_where_polling_stopped(self):
        self.add_samples(self.db, 0, 3600)
        self.add_samples(self.db, 3600 + 240, 7200)  # four minutes late: still one span
        self.add_samples(self.db, 9000, 10800)  # half an hour missing: a new span
        # A span runs through the second of its last poll, hence the + 1.
        self.assertEqual(self.db._coverage(), [(epoch(0), epoch(7200) + 1), (epoch(9000), epoch(10800) + 1)])

    def test_coverage_grows_with_new_samples(self):
        self.add_samples(self.db, 0, 3600)
        self.assertEqual(self.db._coverage(), [(epoch(0), epoch(3600) + 1)])
        self.add_samples(self.db, 3630, 5400)
        self.add_samples(self.db, 9000, 9600)
        grown = self.db._coverage()
        self.assertEqual(grown, [(epoch(0), epoch(5400) + 1), (epoch(9000), epoch(9600) + 1)])
        fresh = Database(self.path)
        self.addCleanup(fresh.close)
        self.assertEqual(fresh._coverage(), grown, "the incremental result matches a full rebuild")

    def test_counts_roll_up_into_quarter_hours(self):
        self.add_samples(self.db, 0, 870, count=4)  # 30 samples in the first quarter hour
        self.add_samples(self.db, 900, 1770, count=6)
        rollup = dict(self.db._counts())
        first = epoch(0) // 900
        self.assertEqual(rollup[first], (30, 120, 4, 4))
        self.assertEqual(rollup[first + 1], (30, 180, 6, 6))
        # More samples land in the newest bucket and in a new one.
        self.db._conn.execute("INSERT INTO count_samples VALUES (?,?,?)", (ts(1790), 9, 10))
        self.add_samples(self.db, 1800, 1830, count=2)
        rollup = self.db._counts()
        self.assertEqual(rollup[first + 1], (31, 189, 6, 9))
        self.assertEqual(rollup[first + 2], (2, 4, 2, 2))
        self.assertEqual(rollup[first], (30, 120, 4, 4))

    def test_presence_events_are_read_once_then_appended(self):
        self.add_device("AA:00:00:00:00:01", "phone", active=False, events=[(60, "new"), (600, "offline")])
        events = self.db._presence_events()
        self.assertEqual(events["AA:00:00:00:00:01"], [(epoch(60), True), (epoch(600), False)])
        self.db._conn.execute("INSERT INTO presence_events VALUES (?,?,?)", (ts(900), "AA:00:00:00:00:01", "online"))
        self.assertEqual(self.db._presence_events()["AA:00:00:00:00:01"][-1], (epoch(900), True))
        self.assertEqual(len(self.db._presence_events()["AA:00:00:00:00:01"]), 3)

    def test_pruning_rebuilds_the_derived_history(self):
        old = (datetime.now(timezone.utc) - T0).total_seconds() - 5 * 86400
        self.add_samples(self.db, 0, 600)
        self.add_samples(self.db, old, old + 600)
        self.assertEqual(len(self.db._coverage()), 2)
        self.assertGreater(self.db._prune(retention_days=10), 0)
        self.assertEqual(len(self.db._coverage()), 1)
        self.assertNotIn(epoch(0) // 900, self.db._counts())

    def test_overview_end_to_end(self):
        self.add_samples(self.db, 0, 7200)
        self.add_device("AA:00:00:00:00:01", "phone", active=True, events=[(1800, "new")])
        self.add_device("AA:00:00:00:00:02", "tv", active=True)
        self.add_device("AA:00:00:00:00:03", "gone", active=False, events=[(600, "offline")])
        out = asyncio.run(self.db.insight(insights.overview, UTC, epoch(7200)))
        self.assertEqual(out["counts"]["connected"], 2)
        self.assertEqual({c["name"] for c in out["connected"]}, {"phone", "tv"})
        self.assertEqual(out["last_sample"], epoch(7200))  # asked in the very second of the last poll
        self.assertEqual(out["series"]["points"][-1], [epoch(7200), 3.0])

    def test_a_device_first_seen_in_the_latest_poll_counts_at_once(self):
        tv, phone = host("AA:00:00:00:00:02", "tv"), host("AA:00:00:00:00:01", "phone")
        poll(self.db, [tv], 0)
        poll(self.db, [tv], 30)
        poll(self.db, [tv, phone], 60)  # the phone joins in the poll the page then reads
        out = asyncio.run(self.db.insight(insights.overview, UTC, epoch(65)))
        self.assertEqual(out["counts"]["connected"], 2)
        self.assertEqual({c["name"] for c in out["connected"]}, {"phone", "tv"})


def host(mac: str, name: str, *, active: bool = True, ip: str = "192.168.2.10") -> dict:
    return {"mac": mac, "name": name, "ip": ip, "interface_type": "WiFi", "active": active,
            "active_last_change": None}


def poll(db: Database, hosts: list[dict], seconds: float):
    """One poll of the router, as if it happened ``seconds`` after T0."""
    with mock.patch("bcdm.db.utc_now_str", return_value=ts(seconds)):
        return asyncio.run(db.apply_poll(hosts))


class PollWriteTests(unittest.TestCase):
    """A poll should write as little as it can: the monitor's home is an SD card."""

    PHONE, TV, PRINTER = "AA:00:00:00:00:01", "AA:00:00:00:00:02", "AA:00:00:00:00:03"

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._dir.name) / "bcdm.db")
        self.addCleanup(self._dir.cleanup)
        self.addCleanup(self.db.close)
        self.hosts = [host(self.PHONE, "phone"), host(self.TV, "tv"),
                      host(self.PRINTER, "printer", active=False)]
        poll(self.db, self.hosts, 0)

    def last_seen(self) -> dict[str, str]:
        return dict(self.db._conn.execute("SELECT mac, last_seen FROM devices"))

    def test_an_unchanged_poll_writes_only_the_count(self):
        before = self.db._conn.total_changes
        poll(self.db, self.hosts, 30)
        self.assertEqual(self.db._conn.total_changes - before, 1)
        self.assertEqual(set(self.last_seen().values()), {ts(0)})

    def test_a_device_that_went_offline_is_written_with_its_event(self):
        before = self.db._conn.total_changes
        events, _ = poll(self.db, [host(self.PHONE, "phone", active=False), *self.hosts[1:]], 30)
        self.assertEqual([(e["mac"], e["event"]) for e in events], [(self.PHONE, "offline")])
        self.assertEqual(self.db._conn.total_changes - before, 3)  # the device, the event, the count
        self.assertEqual(self.last_seen(), {self.PHONE: ts(30), self.TV: ts(0), self.PRINTER: ts(0)})
        self.assertEqual(self.db._conn.execute(
            "SELECT is_active FROM devices WHERE mac = ?", (self.PHONE,)).fetchone()[0], 0)

    def test_a_new_address_or_name_is_written_without_an_event(self):
        events, _ = poll(self.db, [host(self.PHONE, "phone", ip="192.168.2.77"), *self.hosts[1:]], 30)
        self.assertEqual(events, [])
        row = self.db._conn.execute("SELECT last_ip, last_seen FROM devices WHERE mac = ?", (self.PHONE,)).fetchone()
        self.assertEqual(tuple(row), ("192.168.2.77", ts(30)))

    def test_last_seen_is_brought_up_to_date_about_hourly(self):
        poll(self.db, self.hosts, 59 * 60)
        self.assertEqual(set(self.last_seen().values()), {ts(0)})
        poll(self.db, self.hosts, 61 * 60)
        self.assertEqual(set(self.last_seen().values()), {ts(61 * 60)})

    def test_a_device_the_router_stopped_listing_goes_offline(self):
        events, _ = poll(self.db, self.hosts[1:], 30)
        self.assertEqual([(e["mac"], e["event"]) for e in events], [(self.PHONE, "offline")])

    def test_nothing_is_logged_per_connection_type_any_more(self):
        tables = {r[0] for r in self.db._conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertEqual(tables, {"devices", "count_samples", "presence_events"})


class LegacyTableTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._dir.name) / "bcdm.db")
        self.addCleanup(self._dir.cleanup)
        self.addCleanup(self.db.close)
        self.db._conn.executescript(
            f"CREATE TABLE {LEGACY_TABLE} (ts TEXT NOT NULL, interface_type TEXT NOT NULL, active_count INTEGER NOT NULL);"
            f"CREATE INDEX idx_interface_samples_ts ON {LEGACY_TABLE}(ts);"
        )
        self.db._conn.executemany(f"INSERT INTO {LEGACY_TABLE} VALUES (?, 'WiFi', 3)", [(ts(s),) for s in range(0, 3000, 30)])
        self.db._conn.commit()

    def has_table(self) -> bool:
        return self.db._has_table(LEGACY_TABLE)

    def test_it_is_only_dropped_when_told_to(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(asyncio.run(drop_interface_samples(self.db, yes=False)), 0)
        self.assertIn("100 rows", out.getvalue())
        self.assertTrue(self.has_table())

    def test_dropping_frees_its_pages(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            asyncio.run(drop_interface_samples(self.db, yes=True))
        self.assertFalse(self.has_table())
        self.assertIn("Dropped", out.getvalue())
        again = asyncio.run(self.db.drop_legacy_table())
        self.assertEqual(again, {"present": False, "rows": 0, "bytes": 0})

    def test_pruning_still_trims_it_while_it_exists(self):
        self.assertEqual(self.db._prune(retention_days=1), 100)
        self.db._conn.execute(f"DROP TABLE {LEGACY_TABLE}")
        self.assertEqual(self.db._prune(retention_days=1), 0)


class ForgetTests(unittest.TestCase):
    """Forgetting deletes a device's addresses and history; nothing of it may linger."""

    ANN_OLD, ANN_NEW = "A2:00:00:00:00:01", "A2:00:00:00:00:02"
    TV = "44:00:00:00:00:10"
    IPHONE_1, IPHONE_2 = "B2:00:00:00:00:01", "B2:00:00:00:00:02"
    NOW = None

    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "bcdm.db"
        self.db = Database(self.path)
        self.addCleanup(self._dir.cleanup)
        self.addCleanup(self.db.close)
        self.now = epoch(7200)
        conn = self.db._conn
        conn.executemany("INSERT INTO count_samples VALUES (?, 3, 5)", [(ts(s),) for s in range(0, 7201, 30)])
        devices = [
            # Ann's phone changed its address at 3600; only the old one carries her name.
            (self.ANN_OLD, "Ann-s-Pixel", "Ann", 0, 0, [(1800, "offline")]),
            (self.ANN_NEW, "Ann-s-Pixel", None, 3600, 1, [(3600, "new")]),
            (self.TV, "tv", None, 0, 1, []),
            # Two different phones with the same name, online together.
            (self.IPHONE_1, "iPhone", None, 0, 0, [(600, "online"), (1500, "offline")]),
            (self.IPHONE_2, "iPhone", None, 0, 0, [(700, "online"), (1600, "offline")]),
        ]
        for mac, name, owner, first, active, events in devices:
            conn.execute(
                "INSERT INTO devices (mac, name, owner, first_seen, last_seen, is_active) VALUES (?,?,?,?,?,?)",
                (mac, name, owner, ts(first), ts(7200), active),
            )
            conn.executemany("INSERT INTO presence_events VALUES (?,?,?)", [(ts(s), mac, e) for s, e in events])
        conn.commit()

    def names(self) -> set[str]:
        return {d["name"] for d in asyncio.run(self.db.insight(insights.devices, UTC, self.now))["devices"]}

    def rows_for(self, *macs: str) -> tuple[int, int]:
        marks = ",".join("?" * len(macs))
        count = lambda table: self.db._conn.execute(  # noqa: E731
            f"SELECT COUNT(*) FROM {table} WHERE mac IN ({marks})", macs).fetchone()[0]
        return count("devices"), count("presence_events")

    def test_forgetting_an_owner_takes_every_address_of_their_devices(self):
        (match,) = asyncio.run(self.db.find_subjects("owner", "ann", self.now))
        self.assertEqual(match["subject"], "owner:Ann")
        self.assertEqual({a["mac"] for a in match["addresses"]}, {self.ANN_OLD, self.ANN_NEW})
        self.assertEqual(self.rows_for(self.ANN_OLD, self.ANN_NEW), (2, 2), "looking deletes nothing")

        done = asyncio.run(self.db.forget_subject("owner:Ann", self.now))
        self.assertEqual(done, {"name": "Ann", "addresses": 2, "events": 2})
        self.assertEqual(self.rows_for(self.ANN_OLD, self.ANN_NEW), (0, 0))
        self.assertNotIn("Ann-s-Pixel", self.names())
        self.assertEqual(asyncio.run(self.db.insight(insights.devices, UTC, self.now))["owners"], [])

    def test_a_device_is_found_by_any_of_its_addresses_or_its_name(self):
        for text in (self.ANN_OLD.lower(), "ann-s-pixel"):
            (match,) = asyncio.run(self.db.find_subjects("device", text, self.now))
            self.assertEqual(match["subject"], "dev:name:ann-s-pixel")
            self.assertEqual(len(match["addresses"]), 2)

    def test_a_shared_name_matches_several_devices(self):
        matches = asyncio.run(self.db.find_subjects("device", "iPhone", self.now))
        self.assertEqual({m["subject"] for m in matches},
                         {f"dev:mac:{self.IPHONE_1}", f"dev:mac:{self.IPHONE_2}"})
        (one,) = asyncio.run(self.db.find_subjects("device", "iPhone ·00:01", self.now))
        self.assertEqual(one["subject"], f"dev:mac:{self.IPHONE_1}")

    def test_an_unknown_subject_is_refused(self):
        self.assertEqual(asyncio.run(self.db.find_subjects("owner", "Nobody", self.now)), [])
        with self.assertRaises(insights.UnknownSubject):
            asyncio.run(self.db.forget_subject("owner:Nobody", self.now))
        self.assertEqual(self.rows_for(self.ANN_OLD, self.ANN_NEW, self.TV), (3, 2))

    def test_a_forgotten_device_that_returns_starts_from_nothing(self):
        self.names()  # the history is now held in memory
        asyncio.run(self.db.forget_subject("dev:name:ann-s-pixel", self.now))
        events, _ = poll(self.db, [host(self.ANN_NEW, "Ann-s-Pixel"), host(self.TV, "tv")], 7230)
        self.assertEqual([(e["mac"], e["event"]) for e in events], [(self.ANN_NEW, "new")])
        back = next(d for d in asyncio.run(self.db.insight(insights.devices, UTC, epoch(7260)))["devices"]
                    if d["name"] == "Ann-s-Pixel")
        self.assertEqual(back["first_on"], epoch(7230))
        self.assertEqual(len(back["macs"]), 1)

    def test_a_change_made_by_another_process_is_noticed(self):
        self.assertIn("tv", self.names())  # prime what is held in memory
        other = sqlite3.connect(self.path)
        for mac in (self.IPHONE_1, self.IPHONE_2):
            other.execute("DELETE FROM presence_events WHERE mac = ?", (mac,))
            other.execute("DELETE FROM devices WHERE mac = ?", (mac,))
        # ...and one address is listed again, with no history this time.
        other.execute("INSERT INTO devices (mac, name, first_seen, last_seen) VALUES (?,?,?,?)",
                      (self.IPHONE_1, "iPhone", ts(7000), ts(7000)))
        other.commit()
        other.close()
        devices = asyncio.run(self.db.insight(insights.devices, UTC, self.now))["devices"]
        again = next(d for d in devices if any(m["mac"] == self.IPHONE_1 for m in d["macs"]))
        self.assertEqual(again["kind"], "never", "its old history must not come back from memory")

    def test_the_command_only_deletes_with_yes(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(asyncio.run(forget(self.db, "owner", "Ann", yes=False)), 0)
        self.assertIn("2 MAC address(es), 2 recorded events", out.getvalue())
        self.assertIn("Nothing deleted", out.getvalue())
        self.assertEqual(self.rows_for(self.ANN_OLD, self.ANN_NEW), (2, 2))

        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(asyncio.run(forget(self.db, "owner", "Ann", yes=True)), 0)
        self.assertIn("Deleted 2 address(es) and 2 events", out.getvalue())
        self.assertEqual(self.rows_for(self.ANN_OLD, self.ANN_NEW), (0, 0))

    def test_the_command_refuses_an_ambiguous_or_unknown_name(self):
        for kind, text in (("device", "iPhone"), ("owner", "Nobody")):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(asyncio.run(forget(self.db, kind, text, yes=True)), 1)
        self.assertEqual(self.rows_for(self.IPHONE_1, self.IPHONE_2), (2, 4))


class DeviceEditTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._dir.name) / "bcdm.db")
        self.addCleanup(self._dir.cleanup)
        self.addCleanup(self.db.close)
        for mac in ("AA:00:00:00:00:01", "AA:00:00:00:00:02"):
            self.db._conn.execute(
                "INSERT INTO devices (mac, name, first_seen, last_seen) VALUES (?,?,?,?)",
                (mac, "phone", ts(0), ts(0)),
            )
        self.db._conn.commit()

    def owners(self) -> dict[str, str | None]:
        return dict(self.db._conn.execute("SELECT mac, owner FROM devices"))

    def test_one_edit_reaches_every_address_of_a_device(self):
        changed = asyncio.run(self.db.update_devices(["AA:00:00:00:00:01", "AA:00:00:00:00:02"], owner="  Ann "))
        self.assertEqual(changed, 2)
        self.assertEqual(set(self.owners().values()), {"Ann"})

    def test_clearing_an_owner_stores_null(self):
        asyncio.run(self.db.update_devices(["AA:00:00:00:00:01"], owner="Ann"))
        asyncio.run(self.db.update_devices(["AA:00:00:00:00:01"], owner="   "))
        self.assertIsNone(self.owners()["AA:00:00:00:00:01"])

    def test_only_editable_fields_are_written(self):
        changed = asyncio.run(self.db.update_devices(["AA:00:00:00:00:01"], name="hacked", is_active=1))
        self.assertEqual(changed, 0)
        device = asyncio.run(self.db.update_device("AA:00:00:00:00:01", watched=True, hidden=None))
        self.assertTrue(device["watched"])
        self.assertFalse(device["hidden"])
        self.assertEqual(device["router_name"], "phone")

    def test_unknown_device(self):
        self.assertIsNone(asyncio.run(self.db.update_device("FF:FF:FF:FF:FF:FF", owner="Ann")))


class MigrationTests(unittest.TestCase):
    def test_a_database_from_before_owners_gains_the_column(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            old = sqlite3.connect(path)
            old.executescript(
                "CREATE TABLE devices (mac TEXT PRIMARY KEY, name TEXT, label TEXT, last_ip TEXT,"
                " interface_type TEXT, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,"
                " is_active INTEGER NOT NULL DEFAULT 0, watched INTEGER NOT NULL DEFAULT 0,"
                " hidden INTEGER NOT NULL DEFAULT 0);"
                "INSERT INTO devices (mac, name, label, first_seen, last_seen, watched)"
                " VALUES ('AA:00:00:00:00:01', 'phone', 'My phone', '2026-06-19 18:37:29', '2026-06-19 18:37:29', 1);"
            )
            old.commit()
            old.close()

            db = Database(path)
            try:
                device = asyncio.run(db.get_device("AA:00:00:00:00:01"))
                self.assertEqual((device["label"], device["watched"], device["owner"]), ("My phone", True, None))
                asyncio.run(db.update_device("AA:00:00:00:00:01", owner="Ann"))
                self.assertEqual(asyncio.run(db.get_device("AA:00:00:00:00:01"))["owner"], "Ann")
            finally:
                db.close()
            Database(path).close()  # opening it again changes nothing


if __name__ == "__main__":
    unittest.main()
