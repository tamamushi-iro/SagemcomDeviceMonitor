"""Interval math and local-time bucketing behind the dashboard charts.

Run from the project root:  uv run python -m unittest discover -s tests -v
"""

from __future__ import annotations

import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from bcdm.analytics import (
    Track,
    bridge,
    classify,
    complement,
    day_spans,
    day_start,
    half_hour_edges,
    intersect,
    is_randomized_mac,
    longest_low_run,
    looks_portable,
    online_intervals,
    overlap_exceeds,
    short_drops,
    single_device_names,
    suggest_owner,
    union,
    usual_band,
)

TORONTO = ZoneInfo("America/Toronto")
H = 3600


def at(y: int, mo: int, d: int, h: int = 0, mi: int = 0, tz: ZoneInfo = TORONTO) -> int:
    return int(datetime(y, mo, d, h, mi, tzinfo=tz).timestamp())


class OnlineIntervals(unittest.TestCase):
    def test_no_events_means_the_current_state_held_all_along(self):
        self.assertEqual(online_intervals(100, True, [], 500), [(100, 500)])
        self.assertEqual(online_intervals(100, False, [], 500), [])

    def test_first_event_offline_means_online_since_first_seen(self):
        self.assertEqual(online_intervals(100, False, [(200, False)], 500), [(100, 200)])

    def test_first_event_online_means_offline_before_it(self):
        events = [(200, True), (300, False), (400, True)]
        self.assertEqual(online_intervals(100, True, events, 500), [(200, 300), (400, 500)])

    def test_repeated_events_do_not_restart_or_split_an_interval(self):
        events = [(200, True), (250, True), (300, False), (350, False)]
        self.assertEqual(online_intervals(100, False, events, 500), [(200, 300)])

    def test_zero_length_intervals_are_dropped(self):
        self.assertEqual(online_intervals(100, False, [(100, False)], 500), [])


class IntervalLists(unittest.TestCase):
    def test_intersect(self):
        self.assertEqual(intersect([(0, 10), (20, 30)], [(5, 25)]), [(5, 10), (20, 25)])
        self.assertEqual(intersect([(0, 10)], [(10, 20)]), [])

    def test_bridge_merges_only_gaps_within_tolerance(self):
        ivs = [(0, 10), (15, 20), (40, 50)]
        self.assertEqual(bridge(ivs, 5), [(0, 20), (40, 50)])
        self.assertEqual(bridge(ivs, 4), ivs)
        self.assertEqual(bridge(ivs, 20), [(0, 50)])

    def test_union_merges_overlapping_and_touching(self):
        self.assertEqual(union([[(0, 10), (30, 40)], [(5, 15), (40, 45)]]), [(0, 15), (30, 45)])

    def test_complement(self):
        self.assertEqual(complement([(10, 20), (30, 40)], 0, 50), [(0, 10), (20, 30), (40, 50)])
        self.assertEqual(complement([(0, 50)], 10, 20), [])
        self.assertEqual(complement([], 10, 20), [(10, 20)])

    def test_overlap_exceeds_adds_up_pieces(self):
        a, b = [(0, 10), (20, 30)], [(5, 25)]  # overlap 5 + 5
        self.assertTrue(overlap_exceeds(a, b, 9))
        self.assertFalse(overlap_exceeds(a, b, 10))
        self.assertFalse(overlap_exceeds([(0, 10)], [(10, 20)], 0))


class TrackQueries(unittest.TestCase):
    def setUp(self):
        self.track = Track([(10, 20), (30, 50)])

    def test_overlap(self):
        self.assertEqual(self.track.overlap(0, 100), 30)
        self.assertEqual(self.track.overlap(15, 35), 10)
        self.assertEqual(self.track.overlap(20, 30), 0)
        self.assertEqual(self.track.overlap(40, 40), 0)
        self.assertEqual(Track().overlap(0, 100), 0)

    def test_at_is_start_inclusive_end_exclusive(self):
        self.assertTrue(self.track.at(10))
        self.assertFalse(self.track.at(20))
        self.assertFalse(self.track.at(25))

    def test_slice_clips_to_the_window(self):
        self.assertEqual(self.track.slice(15, 40), [(15, 20), (30, 40)])
        self.assertEqual(self.track.slice(20, 30), [])


class ShortDrops(unittest.TestCase):
    def test_counts_brief_monitored_gaps_only(self):
        raw = Track([(0, 100), (160, 300), (1000, 1100), (1200, 1300)])
        cover = Track([(0, 2000)])
        # 60 s gap counts; 700 s gap is an absence; 100 s gap counts.
        self.assertEqual(short_drops(raw, cover, 0, 2000), 2)

    def test_a_gap_the_monitor_did_not_see_is_not_a_drop(self):
        raw = Track([(0, 100), (160, 300)])
        cover = Track([(0, 120), (150, 300)])  # blind for part of the gap
        self.assertEqual(short_drops(raw, cover, 0, 300), 0)


class LocalTime(unittest.TestCase):
    def test_an_ordinary_day_has_48_half_hours(self):
        edges = half_hour_edges(TORONTO, date(2026, 10, 14))
        self.assertEqual(len(edges), 49)
        self.assertEqual({b - a for a, b in zip(edges, edges[1:])}, {1800})
        self.assertEqual(edges[0], day_start(TORONTO, date(2026, 10, 14)))

    def test_clock_change_days_keep_their_true_length(self):
        back = half_hour_edges(TORONTO, date(2026, 11, 1))  # clocks go back: 25 hours
        self.assertEqual(back[-1] - back[0], 25 * H)
        forward = half_hour_edges(TORONTO, date(2026, 3, 8))  # clocks go forward: 23 hours
        self.assertEqual(forward[-1] - forward[0], 23 * H)
        for edges in (back, forward):
            self.assertEqual(list(edges), sorted(edges))

    def test_day_spans_are_wall_clock_minutes(self):
        day = date(2026, 10, 14)
        ivs = [(at(2026, 10, 14, 8, 30), at(2026, 10, 14, 17, 15))]
        self.assertEqual(day_spans(ivs, TORONTO, day), [[510.0, 1035.0]])

    def test_day_spans_reach_the_edges_of_the_day(self):
        day = date(2026, 10, 14)
        ivs = [(at(2026, 10, 14), at(2026, 10, 15))]
        self.assertEqual(day_spans(ivs, TORONTO, day), [[0.0, 1440.0]])

    def test_wall_clock_follows_daylight_saving(self):
        # The same wall-clock hour in summer and winter is a different UTC hour.
        summer = day_spans([(at(2026, 7, 1, 9), at(2026, 7, 1, 10))], TORONTO, date(2026, 7, 1))
        winter = day_spans([(at(2026, 12, 1, 9), at(2026, 12, 1, 10))], TORONTO, date(2026, 12, 1))
        self.assertEqual(summer, winter)
        self.assertEqual(summer, [[540.0, 600.0]])


class LongestLowRun(unittest.TestCase):
    def test_plain_run(self):
        profile = [1.0] * 48
        profile[17:34] = [0.1] * 17  # 08:30 to 17:00
        self.assertEqual(longest_low_run(profile), (17, 17))

    def test_run_wrapping_past_midnight_is_one_run(self):
        profile = [0.1] * 48
        profile[16:36] = [0.9] * 20  # home 08:00 to 18:00, away overnight
        self.assertEqual(longest_low_run(profile), (36, 28))

    def test_never_low_and_always_low(self):
        self.assertIsNone(longest_low_run([0.9] * 48))
        self.assertEqual(longest_low_run([0.1] * 48), (0, 48))

    def test_unknown_cells_break_a_run(self):
        profile = [0.9] * 48
        profile[10:14] = [0.1, 0.1, None, 0.1]
        self.assertEqual(longest_low_run(profile), (10, 2))


class Identity(unittest.TestCase):
    def test_owner_from_possessive_hostnames(self):
        self.assertEqual(suggest_owner("Manan-s-S25-Ultra"), "Manan")
        self.assertEqual(suggest_owner("Sabis-iPhone"), "Sabi")
        self.assertEqual(suggest_owner("dhrumil-s-Pixel"), "Dhrumil")

    def test_no_owner_from_generic_hostnames(self):
        for name in ("iPhone", "Watch", "DESKTOP-TSP57R4", "amazon-5dad600b", "Pixel-7-Pro", "", None):
            self.assertIsNone(suggest_owner(name), name)

    def test_hostnames_of_things_people_carry(self):
        for name in ("iPhone", "Sabis-iPhone", "Watch", "Pixel-7-Pro", "Manan-s-S25-Ultra",
                     "Sabi-s-M31", "Galaxy-S24"):
            self.assertTrue(looks_portable(name), name)
        for name in ("MacBookPro", "LAPTOP-JMRUU528", "DESKTOP-TSP57R4", "iPad", "Galaxy-Tab-S9",
                     "My-ecobee", "amazon-5dad600b5928f7eb", "RPI5-TV", "Parvish", "", None):
            self.assertFalse(looks_portable(name), name)

    def test_randomized_mac(self):
        self.assertTrue(is_randomized_mac("9E:87:F7:98:A3:CF"))
        self.assertTrue(is_randomized_mac("82:89:65:92:83:1A"))
        self.assertFalse(is_randomized_mac("44:61:32:98:26:F7"))

    def test_addresses_that_take_turns_are_one_device(self):
        tracks = {
            "A": Track([(0, 1000)]),
            "B": Track([(1005, 2000)]),  # took over five seconds later
            "C": Track([]),  # listed by the router, never online
        }
        self.assertEqual(single_device_names({"phone": ["A", "B", "C"]}, tracks), {"phone"})

    def test_addresses_online_together_are_different_devices(self):
        tracks = {"A": Track([(0, 5000)]), "B": Track([(1000, 4000)])}
        self.assertEqual(single_device_names({"iphone": ["A", "B"]}, tracks), set())

    def test_a_brief_handover_overlap_is_tolerated(self):
        tracks = {"A": Track([(0, 1000)]), "B": Track([(700, 2000)])}  # 5 minutes together
        self.assertEqual(single_device_names({"phone": ["A", "B"]}, tracks), {"phone"})

    def test_a_name_with_one_address_needs_no_merging(self):
        self.assertEqual(single_device_names({"tv": ["A"]}, {"A": Track([(0, 10)])}), set())


class Classify(unittest.TestCase):
    DAY = 86400
    NOW = 200 * 86400

    def kind(self, **kw):
        base = dict(now=self.NOW, first_on=self.NOW - 60 * self.DAY, last_on=self.NOW,
                    online_s=10 * self.DAY, observed_s=60 * self.DAY,
                    recent_days_seen=20, recent_days_observed=28)
        return classify(**{**base, **kw})

    def test_never(self):
        self.assertEqual(self.kind(first_on=None, last_on=None, online_s=0), "never")

    def test_always_on(self):
        self.assertEqual(self.kind(online_s=57 * self.DAY), "always_on")

    def test_always_on_needs_a_few_days_of_evidence(self):
        self.assertEqual(self.kind(first_on=self.NOW - self.DAY, online_s=self.DAY, observed_s=self.DAY), "new")

    def test_gone(self):
        self.assertEqual(self.kind(last_on=self.NOW - 40 * self.DAY), "gone")

    def test_regular_and_occasional(self):
        self.assertEqual(self.kind(recent_days_seen=14), "regular")
        self.assertEqual(self.kind(recent_days_seen=5), "occasional")


class UsualBand(unittest.TestCase):
    def test_needs_three_weeks(self):
        self.assertIsNone(usual_band([10, 12]))

    def test_small_samples_use_the_full_range(self):
        self.assertEqual(usual_band([12, 10, 11]), (10, 11, 12))

    def test_one_outlier_each_way_is_ignored(self):
        self.assertEqual(usual_band([10, 11, 11, 12, 12, 13, 30, 2]), (10, 11.5, 13))


if __name__ == "__main__":
    unittest.main()
