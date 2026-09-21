#!/usr/bin/env python3
"""Offline tests -- no network, no browser. Run: python tests.py"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bmswatch import config as configmod
from bmswatch.bms import parse_showtimes
from bmswatch.config import Watch
from bmswatch.engine import build_alert
from bmswatch.matcher import filter_shows, matches
from bmswatch.state import State


def payload(date="20260925", venues=None):
    """A trimmed copy of a real showtimes-by-event response."""
    return {
        "ShowDatesArray": [
            {"DateCode": "20260925", "isDisabled": False},
            {"DateCode": "20260926", "isDisabled": False},
            {"DateCode": "20260927", "isDisabled": True},
        ],
        "ShowDetails": [{
            "Date": date,
            "Event": {
                "EventTitle": "The Paradise",
                "ChildEvents": [
                    {"EventCode": "ET001", "EventDimension": "2D",
                     "EventLang": "Telugu", "EventUrl": "the-paradise"},
                    {"EventCode": "ET002", "EventDimension": "IMAX 2D",
                     "EventLang": "Telugu", "EventUrl": "the-paradise"},
                    {"EventCode": "ET003", "EventDimension": "2D",
                     "EventLang": "Hindi", "EventUrl": "the-paradise"},
                ],
            },
            "Venues": venues if venues is not None else [
                {
                    "VenueName": "AMB Cinemas: Gachibowli", "VenueCode": "AMBG",
                    "AllowSales": "Y",
                    "ShowTimes": [
                        {"ShowTime": "09:30 AM", "ShowDateCode": date, "EventCode": "ET002",
                         "Attributes": "IMAX", "Availability": "A", "MinPrice": "390",
                         "MaxPrice": "449",
                         "Categories": [{"SeatsAvail": "120"}, {"SeatsAvail": "30"}]},
                        {"ShowTime": "10:50 PM", "ShowDateCode": date, "EventCode": "ET001",
                         "Attributes": "", "Availability": "S", "MinPrice": "190",
                         "MaxPrice": "190", "Categories": [{"SeatsAvail": "0"}]},
                    ],
                },
                {
                    "VenueName": "Bhramaramba 70MM: Kukatpally", "VenueCode": "BHRA",
                    "AllowSales": "Y",
                    "ShowTimes": [
                        {"ShowTime": "06:00 PM", "ShowDateCode": date, "EventCode": "ET003",
                         "Attributes": "", "Availability": "A", "MinPrice": "150",
                         "MaxPrice": "150", "Categories": [{"SeatsAvail": "88"}]},
                    ],
                },
                {
                    "VenueName": "Closed Cinema: Nowhere", "VenueCode": "CLSD",
                    "AllowSales": "N", "ShowTimes": [
                        {"ShowTime": "01:00 PM", "ShowDateCode": date, "EventCode": "ET001",
                         "Categories": []}],
                },
            ],
        }],
    }


def watch(**kw):
    base = dict(id="w", movie="The Paradise", event_code="ET00436621")
    base.update(kw)
    return Watch(**base)


class TestParsing(unittest.TestCase):
    def test_parses_shows_with_format_and_language(self):
        snap = parse_showtimes(payload(), "ET00436621", "20260925")
        self.assertEqual(snap.title, "The Paradise")
        self.assertEqual(len(snap.shows), 3)            # the AllowSales=N venue is skipped
        imax = snap.shows[0]
        self.assertEqual(imax.fmt, "IMAX 2D")
        self.assertEqual(imax.language, "Telugu")
        self.assertEqual(imax.seats_avail, 150)
        self.assertFalse(imax.sold_out)

    def test_sold_out_detected(self):
        snap = parse_showtimes(payload(), "ET00436621", "20260925")
        self.assertTrue(snap.shows[1].sold_out)

    def test_open_dates_skip_disabled(self):
        snap = parse_showtimes(payload(), "ET00436621", "20260925")
        self.assertEqual(snap.open_dates, ["20260925", "20260926"])

    def test_substitute_date_is_rejected(self):
        """BMS answers an empty date with the next one that has shows."""
        snap = parse_showtimes(payload(date="20261001"), "ET00436621", "20260925")
        self.assertEqual(snap.shows, [])
        self.assertFalse(snap.booking_open)

    def test_book_url(self):
        snap = parse_showtimes(payload(), "ET00436621", "20260925")
        self.assertEqual(
            snap.book_url("hyderabad", "HYD", "20260925"),
            "https://in.bookmyshow.com/buytickets/the-paradise-hyderabad/"
            "movie-hyd-ET00436621-MT/20260925")


class TestMatching(unittest.TestCase):
    def setUp(self):
        self.shows = parse_showtimes(payload(), "ET00436621", "20260925").shows

    def test_theatre_match_is_whole_word(self):
        """'AMB' must not match 'Bhramaramba'."""
        got = filter_shows(watch(theatres=["AMB"]), self.shows)
        self.assertTrue(got)
        self.assertTrue(all("AMB Cinemas" in s.venue for s in got))

    def test_theatre_match_ignores_punctuation(self):
        self.assertTrue(filter_shows(watch(theatres=["amb-cinemas"]), self.shows))

    def test_exclude_theatres(self):
        got = filter_shows(watch(exclude_theatres=["AMB"]), self.shows)
        self.assertEqual([s.venue_code for s in got], ["BHRA"])

    def test_format_matches_dimension_or_attribute(self):
        self.assertEqual(len(filter_shows(watch(formats=["IMAX"]), self.shows)), 1)
        self.assertEqual(len(filter_shows(watch(formats=["4DX"]), self.shows)), 0)

    def test_language_filter(self):
        got = filter_shows(watch(languages=["Hindi"]), self.shows)
        self.assertEqual([s.venue_code for s in got], ["BHRA"])

    def test_require_seats_drops_sold_out(self):
        got = filter_shows(watch(require_seats=True, min_seats=100), self.shows)
        self.assertEqual([s.time for s in got], ["09:30 AM"])

    def test_time_window(self):
        w = watch(time_from=17 * 60, time_to=23 * 60 + 59)
        self.assertEqual(sorted(s.time for s in filter_shows(w, self.shows)),
                         ["06:00 PM", "10:50 PM"])

    def test_empty_filters_match_everything(self):
        self.assertEqual(len(filter_shows(watch(), self.shows)), 3)


class TestConfig(unittest.TestCase):
    def test_date_formats(self):
        self.assertEqual(configmod._date_code("2026-09-25"), "20260925")
        self.assertEqual(configmod._date_code("25-09-2026"), "20260925")
        self.assertEqual(configmod._date_code("20260925"), "20260925")
        with self.assertRaises(configmod.ConfigError):
            configmod._date_code("next friday")

    def test_env_expansion_and_defaults(self):
        import os
        os.environ["BMS_TEST_TOKEN"] = "tok123"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "w.yaml"
            path.write_text(
                "defaults:\n  city: hyderabad\n  poll_seconds: 45\n"
                "telegram:\n  bot_token: ${BMS_TEST_TOKEN}\n  chat_id: '42'\n"
                "watches:\n  - movie: The Paradise\n    event_code: ET1\n"
                "    dates: [2026-09-25]\n    theatres: [Prasads]\n"
                "    time_between: ['18:00', '23:00']\n")
            cfg = configmod.load(path)
        self.assertEqual(cfg.telegram.bot_token, "tok123")
        self.assertTrue(cfg.telegram.configured)
        self.assertEqual(cfg.poll_seconds, 45)
        w = cfg.watches[0]
        self.assertEqual(w.id, "the-paradise")       # auto-generated
        self.assertEqual(w.dates, ["20260925"])
        self.assertEqual((w.time_from, w.time_to), (18 * 60, 23 * 60))

    def test_rejects_hammering_and_duplicate_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "w.yaml"
            path.write_text("defaults:\n  poll_seconds: 5\nwatches:\n"
                            "  - movie: A\n    event_code: E1\n")
            with self.assertRaises(configmod.ConfigError):
                configmod.load(path)
            path.write_text("watches:\n  - id: x\n    movie: A\n    event_code: E1\n"
                            "  - id: x\n    movie: B\n    event_code: E2\n")
            with self.assertRaises(configmod.ConfigError):
                configmod.load(path)

    def test_any_date_falls_back_to_open_dates(self):
        w = watch(dates=[], max_dates=2)
        self.assertEqual(w.resolved_dates(["20260925", "20260926", "20260927"]),
                         ["20260925", "20260926"])


class TestStateAndAlert(unittest.TestCase):
    def test_only_new_shows_alert_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = State(Path(tmp) / "state.json")
            fps = ["a", "b"]
            self.assertEqual(st.new_fingerprints("w", fps), ["a", "b"])
            st.mark_alerted("w", fps)
            st.save()
            self.assertEqual(st.new_fingerprints("w", fps), [])
            self.assertEqual(st.new_fingerprints("w", fps + ["c"]), ["c"])
            # survives a restart
            self.assertEqual(State(Path(tmp) / "state.json").new_fingerprints("w", fps), [])

    def test_alert_message_has_venue_time_and_link(self):
        from bmswatch.engine import CheckResult
        snap = parse_showtimes(payload(), "ET00436621", "20260925")
        w = watch(theatres=["AMB"])
        matched = filter_shows(w, snap.shows)
        result = CheckResult("w", "open", "", matched=matched, fresh=matched, snapshot=snap)
        title, html, plain, url = build_alert(w, result)
        self.assertIn("The Paradise", title)
        self.assertIn("AMB Cinemas: Gachibowli", html)
        self.assertIn("09:30 AM", html)
        self.assertIn("IMAX", html)
        self.assertIn("₹390", plain)
        self.assertIn("sold out", plain)
        self.assertTrue(url.endswith("/20260925"))

    def test_alert_escapes_html(self):
        snap = parse_showtimes(
            payload(venues=[{"VenueName": "Odd <b>Cinema</b> & Co", "VenueCode": "ODD",
                             "AllowSales": "Y", "ShowTimes": [
                                 {"ShowTime": "09:00 AM", "ShowDateCode": "20260925",
                                  "EventCode": "ET001", "Categories": []}]}]),
            "ET00436621", "20260925")
        from bmswatch.engine import CheckResult
        w = watch()
        result = CheckResult("w", "open", "", matched=snap.shows, fresh=snap.shows,
                             snapshot=snap)
        _, html, _, _ = build_alert(w, result)
        self.assertIn("&lt;b&gt;Cinema&lt;/b&gt; &amp; Co", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
