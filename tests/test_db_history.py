"""What the Database derives from stored history for the dashboard, and device edits.

Run from the project root:  uv run python -m unittest discover -s tests -v
"""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from bcdm import insights
from bcdm.db import Database
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
        self.assertEqual(self.db._coverage(), [(epoch(0), epoch(7200)), (epoch(9000), epoch(10800))])

    def test_coverage_grows_with_new_samples(self):
        self.add_samples(self.db, 0, 3600)
        self.assertEqual(self.db._coverage(), [(epoch(0), epoch(3600))])
        self.add_samples(self.db, 3630, 5400)
        self.add_samples(self.db, 9000, 9600)
        grown = self.db._coverage()
        self.assertEqual(grown, [(epoch(0), epoch(5400)), (epoch(9000), epoch(9600))])
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
        self.assertEqual(out["last_sample"], epoch(7200))
        self.assertEqual(out["series"]["points"][-1], [epoch(7200), 3.0])


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
