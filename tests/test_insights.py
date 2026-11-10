"""Dashboard payloads built from a small, hand-made household history.

Run from the project root:  uv run python -m unittest discover -s tests -v

The household (America/Toronto, three weeks in October 2026, no clock change):

- Ann's phone is home from 18:00 to 08:00 every day. It announces itself as
  "Ann-s-Pixel" and switches to a new private MAC address on 16 Oct.
- Ann's laptop never leaves the house; it is on from 07:00 to 23:00.
- Two different phones both call themselves "iPhone" and are online together once.
- A thermostat is always on, apart from a two-hour outage on 20 Oct.
- An access point is always on and has never changed state.
- The router also lists an address that has never been online.

The monitor itself missed four hours on 15 Oct.
"""

from __future__ import annotations

import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from bcdm import insights
from bcdm.analytics import BUCKET_S, SAMPLE_GAP_S
from bcdm.insights import State, UnknownSubject, build_model

TZ = ZoneInfo("America/Toronto")
ANN_1, ANN_2, LAPTOP = "A2:00:00:00:00:01", "A2:00:00:00:00:02", "44:00:00:00:00:30"
IPHONE_1, IPHONE_2 = "B2:00:00:00:00:01", "B2:00:00:00:00:02"
THERMOSTAT, ACCESS_POINT, NEVER = "44:00:00:00:00:10", "44:00:00:00:00:20", "C0:00:00:00:00:99"


def at(month: int, day: int, hour: int = 0, minute: int = 0) -> int:
    return int(datetime(2026, month, day, hour, minute, tzinfo=TZ).timestamp())


START = at(10, 5)  # a Monday
NOW = at(10, 26, 12)  # Monday noon, three weeks later
GAP = (at(10, 15, 10), at(10, 15, 14))
ROTATED = date(2026, 10, 16)


def household(now: int = NOW) -> State:
    def device(mac, name, *, active, first_seen=START, owner=None):
        return {"mac": mac, "name": name, "label": None, "owner": owner, "carried": None, "last_ip": "192.168.2.9",
                "interface_type": "WiFi", "is_active": int(active), "watched": 0, "hidden": 0,
                "first_seen": first_seen, "last_seen": NOW}

    # (ts, went_online). Ann is home at the start, leaves at 08:00 and is back at 18:00.
    # The old address is last used on the morning of the 16th; the new one first
    # appears that evening.
    events: dict[str, list[tuple[int, bool]]] = {ANN_1: [], ANN_2: [], LAPTOP: []}
    for d in range(5, 27):
        events[ANN_1 if d <= ROTATED.day else ANN_2].append((at(10, d, 8), False))
        events[LAPTOP].append((at(10, d, 7), True))
        if d < 26:  # it is noon on the 26th: the phone is not back yet, the laptop still on
            events[ANN_1 if d < ROTATED.day else ANN_2].append((at(10, d, 18), True))
            events[LAPTOP].append((at(10, d, 23), False))
    events[IPHONE_1] = [(at(10, 6, 10), True), (at(10, 6, 20), False)]
    events[IPHONE_2] = [(at(10, 6, 11), True), (at(10, 6, 21), False)]
    events[THERMOSTAT] = [(at(10, 20, 2), False), (at(10, 20, 4), True)]

    cover = [(START, GAP[0]), (GAP[1], NOW)]
    rollup = {}
    for a, b in cover:
        for bucket in range(a // BUCKET_S, b // BUCKET_S):
            hour = datetime.fromtimestamp(bucket * BUCKET_S, TZ).hour
            count = 2 if 8 <= hour < 18 else 4
            rollup[bucket] = (30, 30 * count, count, count)

    return State(
        now=now,
        sample_gap=SAMPLE_GAP_S,
        cover=cover,
        devices=[
            device(ANN_1, "Ann-s-Pixel", active=False, owner="Ann"),
            device(ANN_2, "Ann-s-Pixel", active=False, first_seen=at(10, 16, 18)),
            device(LAPTOP, "LAPTOP-ANN", active=True, owner="Ann"),
            device(IPHONE_1, "iPhone", active=False),
            device(IPHONE_2, "iPhone", active=False),
            device(THERMOSTAT, "My-ecobee", active=True),
            device(ACCESS_POINT, "", active=True),
            device(NEVER, "Boban-s-S23", active=False),
        ],
        events=events,
        rollup=rollup,
    )


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.m = build_model(household(), TZ)

    def test_rotating_addresses_become_one_device(self):
        ann = self.m.entities["name:ann-s-pixel"]
        self.assertEqual({d["mac"] for d in ann.rows}, {ANN_1, ANN_2})
        self.assertEqual(ann.rows[0]["mac"], ANN_2, "the address in use comes first")
        self.assertEqual(ann.name, "Ann-s-Pixel")
        self.assertEqual(ann.owner, "Ann", "an owner set on one address covers the device")
        self.assertEqual(ann.kind, "regular")

    def test_same_named_devices_online_together_stay_apart(self):
        names = sorted(e.name for e in self.m.entities.values() if e.hostname == "iPhone")
        self.assertEqual(names, ["iPhone ·00:01", "iPhone ·00:02"])
        self.assertIn(f"mac:{IPHONE_1}", self.m.entities)

    def test_behaviour_classes(self):
        self.assertEqual(self.m.entities[f"mac:{THERMOSTAT}"].kind, "always_on")
        self.assertEqual(self.m.entities[f"mac:{ACCESS_POINT}"].kind, "always_on")
        self.assertEqual(self.m.entities[f"mac:{NEVER}"].kind, "never")
        self.assertEqual(self.m.entities[f"mac:{IPHONE_1}"].kind, "occasional")

    def test_nothing_is_known_about_unmonitored_time(self):
        for key in (f"mac:{THERMOSTAT}", f"mac:{ACCESS_POINT}"):
            raw = self.m.entities[key].raw
            self.assertEqual(raw.overlap(*GAP), 0)
            self.assertTrue(raw.at(GAP[0] - 1) and raw.at(GAP[1]))

    def test_people_come_from_owners(self):
        self.assertEqual(list(self.m.people), ["Ann"])
        self.assertTrue(self.m.people["Ann"].is_person)

    def test_presence_is_read_from_what_a_person_carries(self):
        ann = self.m.people["Ann"]
        self.assertEqual({e.name for e in ann.entities}, {"Ann-s-Pixel", "LAPTOP-ANN"})
        self.assertEqual([e.name for e in ann.tracking], ["Ann-s-Pixel"], "the laptop stays home")
        self.assertFalse(ann.home.at(at(10, 20, 12)), "at work at noon, though the laptop is on")

    def test_carried_can_be_set_by_hand(self):
        st = household()
        next(d for d in st.devices if d["mac"] == LAPTOP)["carried"] = 1
        ann = build_model(st, TZ).people["Ann"]
        self.assertEqual({e.name for e in ann.tracking}, {"Ann-s-Pixel", "LAPTOP-ANN"})
        self.assertTrue(ann.home.at(at(10, 20, 12)))

    def test_with_nothing_carried_everything_counts(self):
        st = household()
        for d in st.devices:
            if d["name"] == "Ann-s-Pixel":
                d["carried"] = 0
        ann = build_model(st, TZ).people["Ann"]
        self.assertEqual({e.name for e in ann.tracking}, {"Ann-s-Pixel", "LAPTOP-ANN"})

    def test_viewing_the_past_ignores_what_came_later(self):
        m = build_model(household(now=at(10, 10, 12)), TZ)
        self.assertEqual(m.last_sample, at(10, 10, 12))
        self.assertNotIn("name:ann-s-pixel", m.entities, "the second address did not exist yet")
        ann = m.entities[f"mac:{ANN_1}"]
        self.assertEqual(ann.last_on, at(10, 10, 8))
        self.assertTrue(m.entities[f"mac:{THERMOSTAT}"].active)


class OverviewTests(unittest.TestCase):
    def setUp(self):
        self.o = insights.overview(build_model(household(), TZ))

    def test_people_and_counts(self):
        (ann,) = self.o["people"]
        self.assertEqual((ann["name"], ann["state"], ann["since"]), ("Ann", "away", at(10, 26, 8)))
        self.assertFalse(ann["approx"])
        self.assertEqual(ann["today_s"], 8 * 3600)
        self.assertEqual(ann["sub"], "1 device", "the laptop is on, but it is not what tracks her")
        counts = self.o["counts"]
        self.assertEqual(counts["connected"], 3)
        self.assertEqual((counts["people_home"], counts["people_total"]), (0, 1))
        self.assertEqual((counts["always_on"], counts["always_on_offline"]), (2, 0))
        self.assertEqual(counts["new_7d"], 0, "a new address for a known device is not a new device")
        self.assertFalse(self.o["stale"])

    def test_usual_range_comes_from_the_same_hour_in_earlier_weeks(self):
        self.assertEqual(list(self.o["counts"]["usual"]), [2.0, 2.0, 2.0])  # noon on the three Mondays before

    def test_activity_reports_people_not_addresses(self):
        seen = [(a["name"], a["type"], a["t"]) for a in self.o["activity"]]
        self.assertEqual(seen[:3], [
            ("Ann", "left", at(10, 26, 8)),
            ("Ann", "arrived", at(10, 25, 18)),
            ("Ann", "left", at(10, 25, 8)),
        ])
        self.assertEqual(self.o["activity"][1]["after_s"], 10 * 3600)

    def test_connected_lists_what_is_online(self):
        self.assertEqual({c["mac"] for c in self.o["connected"]}, {THERMOSTAT, ACCESS_POINT, LAPTOP})
        by_mac = {c["mac"]: c for c in self.o["connected"]}
        self.assertEqual(by_mac[ACCESS_POINT]["since"], GAP[1])
        self.assertTrue(by_mac[ACCESS_POINT]["since_approx"], "already online when monitoring resumed")
        self.assertEqual(by_mac[THERMOSTAT]["since"], at(10, 20, 4))
        self.assertFalse(by_mac[THERMOSTAT]["since_approx"])

    def test_stale_when_polling_stopped(self):
        o = insights.overview(build_model(household(now=NOW + 3600), TZ))
        self.assertTrue(o["stale"])
        self.assertEqual(o["last_sample"], NOW)
        self.assertEqual(o["window"]["gaps"], [[NOW, NOW + 3600]])


class PatternsTests(unittest.TestCase):
    def setUp(self):
        self.m = build_model(household(), TZ)

    def test_a_person_s_usual_day(self):
        p = insights.patterns(self.m, "owner:Ann", 0)
        self.assertEqual(p["subject"]["kind"], "person")
        self.assertEqual([(m["name"], m["counted"]) for m in p["subject"]["members"]],
                         [("Ann-s-Pixel", True), ("LAPTOP-ANN", False)])
        self.assertEqual(p["stats"]["usual_weekday"], {"kind": "out", "from_min": 480, "to_min": 1080})
        self.assertEqual(p["stats"]["usual_weekend"], {"kind": "out", "from_min": 480, "to_min": 1080})
        self.assertAlmostEqual(p["stats"]["share"], 14 / 24, delta=0.02)
        tuesday = p["week"]["values"][1]
        self.assertEqual((tuesday[3], tuesday[12], tuesday[20]), (1.0, 0.0, 1.0))

    def test_day_rows_carry_presence_and_monitoring_gaps(self):
        p = insights.patterns(self.m, "owner:Ann", 0)
        days = {d["date"]: d for d in p["days"]}
        self.assertEqual(p["days"][0]["date"], "2026-10-26", "newest first")
        self.assertEqual(days["2026-10-14"]["home"], [[0.0, 480.0], [1080.0, 1440.0]])
        self.assertEqual(days["2026-10-14"]["value"], 14.0)
        self.assertEqual(days["2026-10-15"]["gaps"], [[600.0, 840.0]])
        self.assertEqual(days["2026-10-15"]["covered_h"], 20.0)
        # The address changed on the 16th; the day still reads as one unbroken routine.
        self.assertEqual(days["2026-10-16"]["home"], [[0.0, 480.0], [1080.0, 1440.0]])

    def test_period_limits_the_detail_but_not_the_calendar(self):
        p = insights.patterns(self.m, "owner:Ann", 4)
        self.assertEqual(len(p["days"]), 22)  # the history is shorter than four weeks
        self.assertEqual(p["calendar"][0][0], "2026-10-05")
        self.assertAlmostEqual(p["calendar"][1][1], 14 / 24, places=2)

    def test_household_counts(self):
        people = insights.patterns(self.m, "people", 0)
        self.assertEqual(people["subject"]["unit"], "people")
        self.assertEqual(people["week"]["values"][1][3], 1.0)
        self.assertEqual(people["week"]["values"][1][12], 0.0)
        devices = insights.patterns(self.m, "devices", 0)
        self.assertEqual((devices["week"]["min"], devices["week"]["max"]), (2, 4))
        self.assertEqual(devices["stats"]["peak"]["value"], 4)
        day = next(d for d in devices["days"] if d["date"] == "2026-10-15")
        self.assertEqual(day["cells"][20:28], [None] * 8, "unmonitored half hours have no value")
        self.assertEqual((day["low"], day["peak"]), (2, 4))

    def test_people_falls_back_to_devices_without_owners(self):
        st = household()
        for d in st.devices:
            d["owner"] = None
        p = insights.patterns(build_model(st, TZ), "people", 4)
        self.assertEqual(p["subject"]["key"], "devices")

    def test_unknown_subject(self):
        for key in ("owner:Nobody", "dev:mac:00:00:00:00:00:00", "nonsense"):
            with self.assertRaises(UnknownSubject):
                insights.patterns(self.m, key, 4)


class HiddenDeviceTests(unittest.TestCase):
    """Hiding leaves a device out of device lists; totals and Health still include it."""

    @staticmethod
    def hide(st: State, mac: str) -> None:
        next(d for d in st.devices if d["mac"] == mac)["hidden"] = 1

    def test_hidden_devices_are_not_offered_on_patterns(self):
        st = household()
        self.hide(st, THERMOSTAT)
        m = build_model(st, TZ)
        offered = [o["name"] for o in insights.patterns(m, "devices", 4)["options"]["devices"]]
        self.assertNotIn("My-ecobee", offered)
        self.assertIn(ACCESS_POINT, offered)

    def test_a_hidden_device_that_is_open_stays_selectable(self):
        st = household()
        self.hide(st, THERMOSTAT)
        p = insights.patterns(build_model(st, TZ), f"dev:mac:{THERMOSTAT}", 4)
        self.assertEqual(p["subject"]["name"], "My-ecobee")
        self.assertIn("My-ecobee", [o["name"] for o in p["options"]["devices"]])

    def test_a_hidden_newcomer_is_not_counted_as_new(self):
        newcomer = "C0:00:00:00:00:77"
        st = household()
        st.devices.append(dict(st.devices[0], mac=newcomer, name="Guest-Tablet", owner=None,
                               first_seen=at(10, 24, 15)))
        st.events[newcomer] = [(at(10, 24, 15), True), (at(10, 24, 17), False)]
        self.assertEqual(insights.overview(build_model(st, TZ))["counts"]["new_7d"], 1)
        self.hide(st, newcomer)
        self.assertEqual(insights.overview(build_model(st, TZ))["counts"]["new_7d"], 0)

    def test_totals_and_health_still_include_hidden_devices(self):
        st = household()
        self.hide(st, THERMOSTAT)
        m = build_model(st, TZ)
        o = insights.overview(m)
        self.assertEqual(o["counts"]["connected"], 3)
        self.assertEqual(o["counts"]["always_on"], 2)
        self.assertTrue(next(c for c in o["connected"] if c["mac"] == THERMOSTAT)["hidden"])
        self.assertIn("My-ecobee", [a["name"] for a in insights.health(m, 30)["always_on"]])


class DevicesAndHealthTests(unittest.TestCase):
    def setUp(self):
        self.m = build_model(household(), TZ)

    def test_device_table(self):
        d = insights.devices(self.m)
        by_key = {e["key"]: e for e in d["devices"]}
        ann = by_key["dev:name:ann-s-pixel"]
        self.assertTrue(ann["merged"])
        self.assertEqual([m["mac"] for m in ann["macs"]], [ANN_2, ANN_1])
        self.assertIsNone(ann["suggestion"], "already has an owner")
        self.assertEqual(d["owners"], ["Ann"])
        self.assertEqual(d["suggestions"], [], "nothing to suggest for a device that was never here")
        self.assertEqual(d["summary"]["devices"], 6)
        self.assertEqual(d["summary"]["addresses"], 8)
        self.assertEqual((ann["carried"], ann["carried_auto"]), (True, True))
        self.assertFalse(by_key[f"dev:mac:{LAPTOP}"]["carried"])

    def test_owner_suggestions_come_from_hostnames(self):
        st = household()
        for d in st.devices:
            d["owner"] = None
        d = insights.devices(build_model(st, TZ))
        self.assertEqual(d["suggestions"], [{"owner": "Ann", "macs": [ANN_2, ANN_1], "devices": 1}])
        self.assertEqual(d["owners"], [])

    def test_device_days(self):
        d = insights.device_days(self.m, 30)
        self.assertEqual(d["step_days"], 1)
        rows = {r["name"]: r for r in d["devices"]}
        self.assertNotIn("Boban-s-S23", rows)
        self.assertEqual(len(rows["Ann-s-Pixel"]["cells"]), 22)
        self.assertEqual(rows["Ann-s-Pixel"]["cells"][1], [1, 14.0])
        self.assertEqual(len(rows["iPhone ·00:01"]["cells"]), 1)

    def test_health(self):
        h = insights.health(self.m, 30)
        self.assertEqual(h["coverage"]["gaps"], [list(GAP)])
        self.assertEqual(h["coverage"]["longest_s"], 4 * 3600)
        always = {a["name"]: a for a in h["always_on"]}
        thermostat = always["My-ecobee"]
        self.assertEqual((thermostat["outages"], thermostat["longest_s"]), (1, 2 * 3600))
        self.assertEqual(thermostat["down"], [[at(10, 20, 2), at(10, 20, 4)]])
        ap = always[ACCESS_POINT]
        self.assertEqual((ap["outages"], ap["uptime"]), (0, 1.0), "a monitoring gap is not an outage")
        self.assertEqual(h["drops"], [])

    def test_short_drops_are_ranked(self):
        st = household()
        t = at(10, 22, 20)
        st.events[IPHONE_1] += [(t, True)] + [
            (t + 600 * i + off, on) for i in range(1, 6) for off, on in ((0, False), (120, True))
        ] + [(t + 3600, False)]  # five two-minute drops in an hour, then gone
        h = insights.health(build_model(st, TZ), 7)
        (worst,) = h["drops"]
        self.assertEqual((worst["name"], worst["drops"]), ("iPhone ·00:01", 5))


if __name__ == "__main__":
    unittest.main()
