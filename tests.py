#!/usr/bin/env python3
"""Offline tests -- no network, no browser. Run: python tests.py"""

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

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

    def test_explicit_time_window(self):
        w = watch(windows=[(17 * 60, 23 * 60 + 59)])
        self.assertEqual(sorted(s.time for s in filter_shows(w, self.shows)),
                         ["06:00 PM", "10:50 PM"])

    def test_named_slots_select_the_right_shows(self):
        from bmswatch.config import TIME_SLOTS

        def slot(*names):
            return watch(slots=list(names),
                         windows=[(TIME_SLOTS[n]["from"], TIME_SLOTS[n]["to"])
                                  for n in names])

        # shows are 09:30 AM (morning), 06:00 PM (evening), 10:50 PM (night)
        self.assertEqual([s.time for s in filter_shows(slot("morning"), self.shows)],
                         ["09:30 AM"])
        self.assertEqual([s.time for s in filter_shows(slot("evening"), self.shows)],
                         ["06:00 PM"])
        self.assertEqual([s.time for s in filter_shows(slot("night"), self.shows)],
                         ["10:50 PM"])
        self.assertEqual([s.time for s in filter_shows(slot("afternoon"), self.shows)],
                         [])

    def test_multiple_slots_are_a_union(self):
        from bmswatch.config import TIME_SLOTS
        names = ["morning", "night"]
        w = watch(slots=names, windows=[(TIME_SLOTS[n]["from"], TIME_SLOTS[n]["to"])
                                        for n in names])
        self.assertEqual(sorted(s.time for s in filter_shows(w, self.shows)),
                         ["09:30 AM", "10:50 PM"])

    def test_window_may_wrap_past_midnight(self):
        w = watch(windows=[(22 * 60, 2 * 60)])       # 10 PM – 2 AM
        self.assertEqual([s.time for s in filter_shows(w, self.shows)], ["10:50 PM"])

    def test_no_slots_means_any_time(self):
        self.assertEqual(len(filter_shows(watch(), self.shows)), 3)

    def test_empty_filters_match_everything(self):
        self.assertEqual(len(filter_shows(watch(), self.shows)), 3)


class TestConfig(unittest.TestCase):
    def test_date_formats(self):
        self.assertEqual(configmod._date_code("2026-09-25"), "20260925")
        self.assertEqual(configmod._date_code("25-09-2026"), "20260925")
        self.assertEqual(configmod._date_code("20260925"), "20260925")
        with self.assertRaises(configmod.ConfigError):
            configmod._date_code("next friday")

    def test_slots_become_windows(self):
        from bmswatch.config import TIME_SLOTS
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "w.yaml"
            path.write_text("watches:\n  - movie: M\n    event_code: E1\n"
                            "    slots: [evening, Night]\n")
            w = configmod.load(path).watches[0]
        self.assertEqual(w.slots, ["evening", "night"])       # normalised
        self.assertEqual(w.windows, [
            (TIME_SLOTS["evening"]["from"], TIME_SLOTS["evening"]["to"]),
            (TIME_SLOTS["night"]["from"], TIME_SLOTS["night"]["to"])])

    def test_unknown_slot_is_rejected_with_the_valid_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "w.yaml"
            path.write_text("watches:\n  - movie: M\n    event_code: E1\n"
                            "    slots: [teatime]\n")
            with self.assertRaises(configmod.ConfigError) as caught:
                configmod.load(path)
        self.assertIn("morning", str(caught.exception))

    def test_describe_slots(self):
        self.assertEqual(configmod.describe_slots([]), "Any time")
        self.assertIn("Evening", configmod.describe_slots(["evening"]))

    def test_default_city_is_available_before_any_watch_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "w.yaml"
            path.write_text("defaults:\n  city: bengaluru\n  region_code: bang\n")
            cfg = configmod.load(path)
        self.assertEqual(cfg.watches, [])
        self.assertEqual(cfg.default_city, "bengaluru")
        self.assertEqual(cfg.default_region, "BANG")

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
        self.assertEqual(w.windows, [(18 * 60, 23 * 60)])

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


class TestFetcher(unittest.TestCase):
    def test_start_is_idempotent(self):
        """make_session starts a session to probe it; the pool starts it again."""
        from bmswatch.fetcher import HTTPSession
        s = HTTPSession(city="hyderabad")
        s.start()
        first = s._session
        s.start()
        self.assertIs(s._session, first)
        s.close()
        self.assertIsNone(s._session)

    def test_showtimes_url_carries_the_filters(self):
        from bmswatch.fetcher import showtimes_url
        url = showtimes_url("ET001", "20260925", "HYD", "1.2.3", "17.4", "78.5")
        for expected in ("eventCode=ET001", "dateCode=20260925",
                         "regionCode=HYD", "subRegion=HYD"):
            self.assertIn(expected, url)

    def test_browser_options_dropped_for_http_engine(self):
        from bmswatch.fetcher import HTTPSession, make_session
        s = make_session(engine="http", city="hyderabad", channel="chrome",
                         window="minimized", profile_dir="/tmp/nope")
        self.assertIsInstance(s, HTTPSession)


class TestNotifier(unittest.TestCase):
    """The bot loop calls these directly; a missing one only shows up when a
    real alert fires, which is the worst possible time to find out."""

    def _notifier(self, telegram=None):
        from bmswatch.config import DesktopConfig, TelegramConfig
        from bmswatch.notify import Notifier
        n = Notifier(TelegramConfig(bot_token="t", chat_id="1", repeat_count=3,
                                    repeat_every_seconds=1),
                     DesktopConfig(enabled=False))
        n.telegram = telegram if telegram is not None else FakeTelegram()
        return n

    def test_alert_once_signature_matches_how_the_bot_calls_it(self):
        import inspect
        from bmswatch.notify import Notifier
        sig = inspect.signature(Notifier.alert_once)
        sig.bind(None, "wid", "title", "<b>body</b>", "plain", "https://x/",
                 round_no=2, rounds=8, chat_id="42")

    def test_alert_signature_accepts_chat_id(self):
        import inspect
        from bmswatch.notify import Notifier
        inspect.signature(Notifier.alert).bind(
            None, "wid", "title", "body", "plain", "https://x/", chat_id="42")

    def test_alert_once_sends_to_the_right_chat_with_buttons(self):
        tg = FakeTelegram()
        self._notifier(tg).alert_once("wid", "T", "<b>body</b>", "plain",
                                      "https://book/", chat_id="4242")
        self.assertEqual(len(tg.sent), 1)
        msg = tg.sent[0]
        self.assertEqual(msg["chat_id"], "4242")
        labels = [b["text"] for row in msg["keyboard"] for b in row]
        self.assertTrue(any("Book now" in l for l in labels))
        self.assertTrue(any("Got it" in l for l in labels))
        data = [b.get("callback_data") for row in msg["keyboard"] for b in row]
        self.assertIn("ack:wid", data)

    def test_later_rounds_are_marked_as_reminders(self):
        tg = FakeTelegram()
        n = self._notifier(tg)
        n.alert_once("wid", "T", "body", "plain", "u", round_no=1, rounds=8)
        n.alert_once("wid", "T", "body", "plain", "u", round_no=3, rounds=8)
        self.assertNotIn("Reminder", tg.sent[0]["text"])
        self.assertIn("Reminder 3/8", tg.sent[1]["text"])

    def test_alert_sends_every_ping_of_every_burst(self):
        tg = FakeTelegram()
        n = self._notifier(tg)
        n.repeat_count, n.burst_size = 3, 3        # 3 bursts of 3
        n.repeat_every, n.burst_gap = 5, 1
        with mock.patch("bmswatch.notify.time.sleep"):
            n.alert("wid", "T", "body", "plain", "u")
        self.assertEqual(len(tg.sent), 9)

    def test_alert_stops_early_once_acknowledged(self):
        tg = FakeTelegram()
        tg.acked = True
        n = self._notifier(tg)
        n.repeat_every, n.burst_gap = 5, 1
        with mock.patch("bmswatch.notify.time.sleep"):
            n.alert("wid", "T", "body", "plain", "u")
        self.assertEqual(len(tg.sent), 2)          # first ping, then the "stopped" note

    def test_engine_delivers_through_the_notifier_without_a_sink(self):
        """handle_result's default path must match the Notifier's signature."""
        from bmswatch.engine import CheckResult, handle_result
        from bmswatch.config import Watch
        tg = FakeTelegram()
        n = self._notifier(tg)
        n.repeat_count = 1
        snap = parse_showtimes(payload(), "ET00436621", "20260925")
        w = Watch(id="w", movie="M", chat_id="777")
        result = CheckResult("w", "open", "", matched=snap.shows,
                             fresh=snap.shows, snapshot=snap)
        n.repeat_count, n.burst_size = 1, 1
        with tempfile.TemporaryDirectory() as tmp:
            handle_result(w, result, State(Path(tmp) / "s.json"), n, verbose=False)
        self.assertEqual(tg.sent[0]["chat_id"], "777")

    def test_alert_sink_overrides_delivery(self):
        from bmswatch.engine import CheckResult, handle_result
        from bmswatch.config import Watch
        tg = FakeTelegram()
        n = self._notifier(tg)
        captured = []
        snap = parse_showtimes(payload(), "ET00436621", "20260925")
        w = Watch(id="w", movie="M", chat_id="777")
        result = CheckResult("w", "open", "", matched=snap.shows,
                             fresh=snap.shows, snapshot=snap)
        with tempfile.TemporaryDirectory() as tmp:
            handle_result(w, result, State(Path(tmp) / "s.json"), n, verbose=False,
                          alert_sink=lambda *a: captured.append(a))
        self.assertEqual(len(captured), 1)
        self.assertEqual(len(tg.sent), 0)          # nothing sent directly


class TestAlertScheduling(unittest.TestCase):
    """Repeats must survive across runs and never block, so that a long
    interval (30 minutes) works in one-shot mode and in CI."""

    def _watch_py(self):
        import importlib.util
        if "watchcli" in sys.modules:
            return sys.modules["watchcli"]
        spec = importlib.util.spec_from_file_location(
            "watchcli", str(Path(__file__).resolve().parent / "watch.py"))
        mod = importlib.util.module_from_spec(spec)
        sys.modules["watchcli"] = mod          # so mock.patch can find it
        spec.loader.exec_module(mod)
        return mod

    def _settings(self, bursts=3, every=1800, size=3, gap=20):
        from bmswatch.config import Settings, TelegramConfig
        return Settings(telegram=TelegramConfig(
            bot_token="t", chat_id="9", repeat_count=bursts,
            repeat_every_seconds=every, burst_size=size, burst_gap_seconds=gap))

    def _record(self, settings, watch_id="w"):
        from bmswatch.config import Watch
        cli = self._watch_py()
        return cli._alert_record(Watch(id=watch_id, movie="M", chat_id="9"),
                                 "T", "b", "p", "u", settings)

    def _notifier(self, tg):
        from bmswatch.config import DesktopConfig, TelegramConfig
        from bmswatch.notify import Notifier
        n = Notifier(TelegramConfig(bot_token="t", chat_id="9"),
                     DesktopConfig(enabled=False))
        n.telegram = tg
        return n

    def test_gap_is_short_inside_a_burst_and_long_between_bursts(self):
        cli = self._watch_py()
        tg = FakeTelegram()
        settings = self._settings(bursts=3, every=1800, size=3, gap=20)
        n = self._notifier(tg)
        pending = [self._record(settings)]

        now = time.time()
        pending = cli._send_due(pending, n, settings)          # ping 1 of burst 1
        self.assertEqual(len(tg.sent), 1)
        self.assertLess(pending[0]["next_at"] - now, 30)       # quick gap

        pending[0]["next_at"] = 0
        pending = cli._send_due(pending, n, settings)          # ping 2
        self.assertLess(pending[0]["next_at"] - time.time(), 30)

        pending[0]["next_at"] = 0
        pending = cli._send_due(pending, n, settings)          # ping 3 ends burst 1
        self.assertEqual(len(tg.sent), 3)
        self.assertGreater(pending[0]["next_at"] - time.time(), 1000)   # long wait

    def test_three_bursts_of_three_is_nine_pings_then_silence(self):
        cli = self._watch_py()
        tg = FakeTelegram()
        settings = self._settings(bursts=3, size=3)
        n = self._notifier(tg)
        pending = [self._record(settings)]
        for _ in range(20):                      # force every gap to elapse
            for item in pending:
                item["next_at"] = 0.0
            pending = cli._send_due(pending, n, settings)
            if not pending:
                break
        self.assertEqual(len(tg.sent), 9)
        self.assertEqual(pending, [])

    def test_reminder_header_counts_bursts_not_pings(self):
        cli = self._watch_py()
        tg = FakeTelegram()
        settings = self._settings(bursts=3, size=3)
        n = self._notifier(tg)
        pending = [self._record(settings)]
        for _ in range(20):
            for item in pending:
                item["next_at"] = 0.0
            pending = cli._send_due(pending, n, settings)
            if not pending:
                break
        texts = [m["text"] for m in tg.sent]
        self.assertFalse(any("Reminder" in t for t in texts[:3]))   # burst 1 is fresh
        self.assertTrue(all("Reminder 2/3" in t for t in texts[3:6]))
        self.assertTrue(all("Reminder 3/3" in t for t in texts[6:9]))

    def test_one_shot_run_finishes_the_burst_but_not_the_long_wait(self):
        """A cron/CI run comes back minutes later; a 20s burst must not be
        smeared across separate runs."""
        cli = self._watch_py()
        tg = FakeTelegram()
        settings = self._settings(bursts=3, every=1800, size=3, gap=20)
        n = self._notifier(tg)
        pending = [self._record(settings)]

        slept = []
        with mock.patch("watchcli.time.sleep", side_effect=slept.append):
            pending = cli._send_due(pending, n, settings, budget=120.0)

        self.assertEqual(len(tg.sent), 3)          # the whole first burst
        self.assertEqual(slept, [20, 20])          # only the short gaps
        self.assertEqual(pending[0]["sent"], 3)
        self.assertGreater(pending[0]["next_at"] - time.time(), 1000)   # burst 2 later

    def test_zero_budget_sends_only_what_is_due(self):
        cli = self._watch_py()
        tg = FakeTelegram()
        settings = self._settings(bursts=3, size=3, gap=20)
        n = self._notifier(tg)
        with mock.patch("watchcli.time.sleep") as slept:
            pending = cli._send_due([self._record(settings)], n, settings)
        self.assertEqual(len(tg.sent), 1)
        slept.assert_not_called()

    def test_budget_too_small_stops_mid_burst_and_resumes_later(self):
        cli = self._watch_py()
        tg = FakeTelegram()
        settings = self._settings(bursts=2, size=3, gap=20)
        n = self._notifier(tg)
        pending = [self._record(settings)]
        with mock.patch("watchcli.time.sleep"):
            pending = cli._send_due(pending, n, settings, budget=20.0)
        self.assertEqual(len(tg.sent), 2)          # only one short gap affordable
        self.assertEqual(pending[0]["sent"], 2)

    def test_send_due_survives_a_failing_send(self):
        cli = self._watch_py()
        tg, settings = FakeTelegram(), self._settings()
        n = self._notifier(tg)
        with mock.patch.object(n, "alert_once", side_effect=RuntimeError("boom")):
            pending = cli._send_due([self._record(settings)], n, settings)
        self.assertEqual(pending[0]["sent"], 1)      # counted, not retried forever

    def test_pending_round_trips_through_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            st = State(path)
            st.set_pending([{"watch_id": "w", "sent": 1, "next_at": 123.0}])
            st.mark_alerted("w", ["fp1"])
            st.save()

            again = State(path)
            self.assertEqual(again.pending()[0]["watch_id"], "w")
            self.assertEqual(again.new_fingerprints("w", ["fp1"]), [])
            again.set_pending([])
            again.save()
            self.assertEqual(State(path).pending(), [])

    def test_pending_key_cannot_collide_with_a_watch(self):
        with tempfile.TemporaryDirectory() as tmp:
            st = State(Path(tmp) / "state.json")
            with self.assertRaises(KeyError):
                st.mark_alerted("__pending__", ["x"])


class TestTelegram(unittest.TestCase):
    def _telegram(self):
        from bmswatch.config import TelegramConfig
        from bmswatch.notify import Telegram
        return Telegram(TelegramConfig(bot_token="tok", chat_id="1"), timeout=15)

    def test_http_timeout_outlasts_long_poll(self):
        """getUpdates holds the socket open; a shorter read timeout kills it."""
        tg = self._telegram()
        with mock.patch("bmswatch.notify.requests.post") as post:
            post.return_value.json.return_value = {"ok": True, "result": []}
            tg._call("getUpdates", offset=1, timeout=20)
        self.assertGreater(post.call_args.kwargs["timeout"], 20)

    def test_plain_calls_keep_the_default_timeout(self):
        tg = self._telegram()
        with mock.patch("bmswatch.notify.requests.post") as post:
            post.return_value.json.return_value = {"ok": True, "result": {}}
            tg._call("getMe")
        self.assertEqual(post.call_args.kwargs["timeout"], 15)

    def test_network_errors_never_leak_the_token(self):
        tg = self._telegram()
        with mock.patch("bmswatch.notify.requests.post",
                        side_effect=Exception("failed: https://api.telegram.org/bottok/getMe")):
            with self.assertRaises(RuntimeError) as caught:
                tg._call("getMe")
        self.assertNotIn("bottok", str(caught.exception))
        self.assertIn("<token>", str(caught.exception))


REGIONS_FIXTURE = {"BookMyShow": {
    "TopCities": [
        {"RegionName": "Hyderabad", "RegionCode": "HYD", "RegionSlug": "hyderabad",
         "Lat": "17.385044", "Long": "78.486671", "StateName": "Telangana",
         "Alias": ["hyderabad", "secunderabad"], "AllowSales": "Y"},
        {"RegionName": "Mumbai", "RegionCode": "MUMBAI", "RegionSlug": "mumbai",
         "Lat": "19.076", "Long": "72.8777", "StateName": "Maharashtra",
         "Alias": ["bombay"], "AllowSales": "Y"},
    ],
    "OtherCities": [
        {"RegionName": "Warangal", "RegionCode": "WARA", "RegionSlug": "warangal",
         "Lat": "17.9689", "Long": "79.5941", "StateName": "Telangana",
         "Alias": ["warangal"], "AllowSales": "Y"},
        {"RegionName": "Nowhere", "RegionCode": "NOPE", "RegionSlug": "nowhere",
         "Lat": "1.0", "Long": "1.0", "StateName": "", "Alias": [], "AllowSales": "N"},
    ]}}

LISTING_FIXTURE = (
    '<a href="https://in.bookmyshow.com/movies/hyderabad/the-paradise/ET00436621">'
    '<div data-content="The Paradise"></div></a>'
    '<a href="https://in.bookmyshow.com/movies/hyderabad/devara-part-1/ET00310216">'
    '<div data-content="Devara - Part 1"></div></a>'
)


class FakeTelegram:
    """Records what the bot would send, and lets tests read it back."""

    def __init__(self):
        self.log = []          # sends and edits together, in order
        self._offset = None
        self._mid = 100

    @property
    def sent(self):
        return [m for m in self.log if m["kind"] == "send"]

    def send(self, text, keyboard=None, reply_markup=None, chat_id=None, **kw):
        self._mid += 1
        self.log.append({"kind": "send", "text": text, "keyboard": keyboard,
                         "reply_markup": reply_markup, "chat_id": chat_id,
                         "message_id": self._mid})
        return {"message_id": self._mid}

    def edit(self, chat_id, message_id, text, keyboard=None):
        self.log.append({"kind": "edit", "text": text, "keyboard": keyboard,
                         "message_id": message_id})

    def answer_callback(self, callback_id, text=""):
        pass

    def get_updates(self, offset=None, timeout=0, allowed=None):
        return []

    def drain(self):
        pass

    def check_ack(self, watch_id):
        return self.acked

    acked = False

    # helpers
    @property
    def last(self):
        return self.log[-1] if self.log else {}

    def last_with_buttons(self):
        for m in reversed(self.log):
            if m.get("keyboard"):
                return m
        return {}

    def last_text(self):
        return self.last.get("text", "")

    def buttons(self):
        rows = self.last_with_buttons().get("keyboard") or []
        return [b for row in rows for b in row]

    def press(self, contains):
        """callback_data of the first button whose label contains `contains`."""
        for b in self.buttons():
            if contains.lower() in b.get("text", "").lower():
                return b.get("callback_data")
        raise AssertionError(f"no button matching {contains!r} in {self.buttons()}")


class FakeFetcher:
    def __init__(self, *a, **kw):
        self.closed = False

    def start(self): pass

    def close(self): self.closed = True

    def _get(self, url, headers=None):
        return 200, json.dumps(REGIONS_FIXTURE)

    def page_html(self, url):
        return LISTING_FIXTURE if "upcoming" not in url else ""

    def showtimes(self, event_code, date_code):
        return payload(date="20260925")


class TestCatalogue(unittest.TestCase):
    def test_parses_titles_and_skips_unsellable_cities(self):
        from bmswatch.catalogue import _parse_regions, parse_movie_listing
        regions = _parse_regions(REGIONS_FIXTURE)
        self.assertEqual([r.code for r in regions], ["HYD", "MUMBAI", "WARA"])
        movies = parse_movie_listing(LISTING_FIXTURE, "hyderabad")
        self.assertEqual([m.title for m in movies], ["The Paradise", "Devara - Part 1"])

    def test_nearest_city_and_alias_search(self):
        from bmswatch.catalogue import _parse_regions, nearest_regions, search_regions
        regions = _parse_regions(REGIONS_FIXTURE)
        self.assertEqual(nearest_regions(regions, 17.44, 78.35)[0][0].code, "HYD")
        self.assertEqual(search_regions(regions, "bombay")[0].code, "MUMBAI")


class TestStore(unittest.TestCase):
    def test_add_list_remove_and_unique_ids(self):
        from bmswatch.store import WatchStore
        with tempfile.TemporaryDirectory() as tmp:
            st = WatchStore(Path(tmp) / "w.json")
            a = st.add({"movie": "The Paradise", "chat_id": "1", "event_code": "E1"})
            b = st.add({"movie": "The Paradise", "chat_id": "1", "event_code": "E2"})
            self.assertNotEqual(a, b)
            self.assertEqual(len(st.watches("1")), 2)
            self.assertEqual(len(st.watches("2")), 0)
            self.assertTrue(st.remove(a))
            self.assertEqual(len(st.watches("1")), 1)
            # survives a reload
            self.assertEqual(len(WatchStore(Path(tmp) / "w.json").watches("1")), 1)

    def test_duplicate_detection(self):
        from bmswatch.store import WatchStore
        with tempfile.TemporaryDirectory() as tmp:
            st = WatchStore(Path(tmp) / "w.json")
            st.add({"movie": "M", "chat_id": "1", "event_code": "E1",
                    "theatres": ["AMB"], "dates": ["20260925"]})
            self.assertIsNotNone(st.duplicate_of("1", "E1", ["AMB"], ["20260925"]))
            self.assertIsNone(st.duplicate_of("1", "E1", ["PVR"], ["20260925"]))

    def test_config_merges_store_watches(self):
        from bmswatch import config as cfg
        from bmswatch.store import WatchStore
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "watchlist.yaml").write_text(
                "watches:\n  - movie: From YAML\n    event_code: ET1\n")
            st = WatchStore(root / "watches.json")
            st.add({"movie": "From Telegram", "chat_id": "42", "event_code": "ET2",
                    "city": "hyderabad", "region_code": "HYD"})
            loaded = cfg.load(root / "watchlist.yaml")
        names = sorted(w.movie for w in loaded.watches)
        self.assertEqual(names, ["From Telegram", "From YAML"])
        bot_watch = [w for w in loaded.watches if w.movie == "From Telegram"][0]
        self.assertEqual(bot_watch.chat_id, "42")

    def test_watchlist_can_be_store_only(self):
        from bmswatch import config as cfg
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "watchlist.yaml").write_text("defaults:\n  city: hyderabad\n")
            self.assertEqual(cfg.load(root / "watchlist.yaml").watches, [])


class TestBotFlow(unittest.TestCase):
    """Walk the whole conversation with fakes -- no network, no Telegram."""

    def setUp(self):
        from bmswatch.bot import Bot
        from bmswatch.store import WatchStore
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.tg = FakeTelegram()
        self.store = WatchStore(root / "watches.json")
        self.changes = []
        self.bot = Bot(self.tg, self.store, lambda c, r: FakeFetcher(),
                       root / ".cache", on_change=lambda: self.changes.append(1))
        self.chat = "42"

    def tearDown(self):
        self.tmp.cleanup()

    def text(self, body):
        self.bot._dispatch({"update_id": 1, "message": {
            "chat": {"id": int(self.chat)}, "text": body}})

    def location(self, lat, lon):
        self.bot._dispatch({"update_id": 2, "message": {
            "chat": {"id": int(self.chat)},
            "location": {"latitude": lat, "longitude": lon}}})

    def click(self, data):
        self.bot._dispatch({"update_id": 3, "callback_query": {
            "id": "cb", "data": data,
            "message": {"message_id": 999, "chat": {"id": int(self.chat)}}}})

    def test_start_asks_for_a_city_when_none_is_set(self):
        self.text("/start")
        joined = " ".join(m["text"] for m in self.tg.sent)
        self.assertIn("Which city", joined)
        self.assertIn("request_location",
                      json.dumps([m.get("reply_markup") for m in self.tg.sent]))

    def test_location_offers_nearest_city(self):
        self.location(17.44, 78.35)
        self.assertIn("Hyderabad", self.tg.last_text() + json.dumps(self.tg.buttons()))

    def test_full_flow_creates_a_watch(self):
        self.location(17.44, 78.35)              # → nearest cities
        self.click(self.tg.press("Hyderabad"))   # → city set, movie list appears
        self.click(self.tg.press("Paradise"))    # → dates come first
        self.assertIn("Which dates", self.tg.last_text())
        self.click(self.tg.press("Any date"))

        # theatres are listed for the dates just chosen
        self.assertIn("Which theatres", self.tg.last_text())
        self.click(self.tg.press("AMB"))
        self.click(self.tg.press("Done"))

        # the timings step
        self.assertIn("Which show timings", self.tg.last_text())
        self.click(self.tg.press("Evening"))
        self.click(self.tg.press("Night"))
        self.click(self.tg.press("Done"))
        self.assertIn("Create this alert", self.tg.last_text())
        self.assertIn("Evening", self.tg.last_text())
        self.click("ok")

        watches = self.store.watches(self.chat)
        self.assertEqual(len(watches), 1)
        w = watches[0]
        self.assertEqual(w["event_code"], "ET00436621")
        self.assertEqual(w["region_code"], "HYD")
        self.assertEqual(w["theatres"], ["AMB Cinemas: Gachibowli"])
        self.assertEqual(w["dates"], [])
        self.assertEqual(w["slots"], ["evening", "night"])
        self.assertTrue(self.changes, "the runner should be told to reload")

    def test_multiple_alerts_and_removal(self):
        self.location(17.44, 78.35)
        self.click(self.tg.press("Hyderabad"))
        for movie in ("Paradise", "Devara"):
            self.text("/add")
            self.click(self.tg.press(movie))
            self.click(self.tg.press("Any date"))
            self.click(self.tg.press("Any theatre"))
            self.click(self.tg.press("Any time"))
            self.click("ok")
        self.assertEqual(len(self.store.watches(self.chat)), 2)

        self.text("/list")
        self.assertIn("Your alerts", self.tg.last_text())
        self.click(self.tg.press("Paradise"))            # the 🗑 button
        remaining = [w["movie"] for w in self.store.watches(self.chat)]
        self.assertEqual(remaining, ["Devara - Part 1"])

    def test_identical_alert_is_not_duplicated(self):
        self.location(17.44, 78.35)
        self.click(self.tg.press("Hyderabad"))
        for _ in range(2):
            self.text("/add")
            self.click(self.tg.press("Paradise"))
            self.click(self.tg.press("Any date"))
            self.click(self.tg.press("Any theatre"))
            self.click(self.tg.press("Any time"))
            self.click("ok")
        self.assertEqual(len(self.store.watches(self.chat)), 1)
        self.assertIn("already have an identical", self.tg.last_text())

    def test_same_movie_different_timings_is_not_a_duplicate(self):
        self.location(17.44, 78.35)
        self.click(self.tg.press("Hyderabad"))
        for slot in ("Morning", "Night"):
            self.text("/add")
            self.click(self.tg.press("Paradise"))
            self.click(self.tg.press("Any date"))
            self.click(self.tg.press("Any theatre"))
            self.click(self.tg.press(slot))
            self.click(self.tg.press("Done"))
            self.click("ok")
        saved = self.store.watches(self.chat)
        self.assertEqual(len(saved), 2)
        self.assertEqual(sorted(w["slots"][0] for w in saved), ["morning", "night"])

    def test_list_shows_the_chosen_timings(self):
        self.location(17.44, 78.35)
        self.click(self.tg.press("Hyderabad"))
        self.text("/add")
        self.click(self.tg.press("Paradise"))
        self.click(self.tg.press("Any date"))
        self.click(self.tg.press("Any theatre"))
        self.click(self.tg.press("Evening"))
        self.click(self.tg.press("Done"))
        self.click("ok")
        self.text("/list")
        self.assertIn("Evening", self.tg.last_text())

    def test_removing_a_watch_signals_the_runner(self):
        """The loop prunes in-flight alerts on this signal, so it must fire."""
        self.location(17.44, 78.35)
        self.click(self.tg.press("Hyderabad"))
        self.text("/add")
        self.click(self.tg.press("Paradise"))
        self.click(self.tg.press("Any date"))
        self.click(self.tg.press("Any theatre"))
        self.click(self.tg.press("Any time"))
        self.click("ok")
        watch_id = self.store.watches(self.chat)[0]["id"]

        self.changes.clear()
        self.click(f"rm:{watch_id}")
        self.assertEqual(self.store.watches(self.chat), [])
        self.assertTrue(self.changes, "removal must tell the runner to reload")

    def test_theatres_are_listed_for_the_dates_chosen(self):
        """The old flow asked for theatres first and listed them from one
        arbitrary date, so you could pick a cinema with no shows on your
        dates and the alert would silently never match."""
        self.location(17.44, 78.35)
        self.click(self.tg.press("Hyderabad"))
        self.click(self.tg.press("Paradise"))
        self.assertIn("Which dates", self.tg.last_text())      # dates first
        self.click(self.tg.press("Any date"))
        self.assertIn("Which theatres", self.tg.last_text())
        labels = [b["text"] for b in self.tg.buttons()]
        self.assertTrue(any("AMB" in l for l in labels))

    def test_confirm_reports_how_many_shows_already_match(self):
        self.location(17.44, 78.35)
        self.click(self.tg.press("Hyderabad"))
        self.click(self.tg.press("Paradise"))
        self.click(self.tg.press("Any date"))
        self.click(self.tg.press("Any theatre"))
        self.click(self.tg.press("Any time"))
        self.assertIn("already match", self.tg.last_text())

    def test_confirm_warns_when_the_timings_exclude_everything(self):
        self.location(17.44, 78.35)
        self.click(self.tg.press("Hyderabad"))
        self.click(self.tg.press("Paradise"))
        self.click(self.tg.press("Any date"))
        self.click(self.tg.press("Any theatre"))
        # fixture shows are 09:30, 18:00 and 22:50 — nothing in the afternoon
        self.click(self.tg.press("Afternoon"))
        self.click(self.tg.press("Done"))
        self.assertIn("No show matches your timings yet", self.tg.last_text())

    def test_confirm_offers_a_way_back_to_dates(self):
        self.location(17.44, 78.35)
        self.click(self.tg.press("Hyderabad"))
        self.click(self.tg.press("Paradise"))
        self.click(self.tg.press("Any date"))
        self.click(self.tg.press("Any theatre"))
        self.click(self.tg.press("Any time"))
        self.click(self.tg.press("Change dates"))
        self.assertIn("Which dates", self.tg.last_text())

    def test_ack_button_is_recorded_for_the_scheduler(self):
        self.click("ack:some-watch")
        self.assertIn("some-watch", self.bot.acked)

    def test_unknown_callback_does_not_raise(self):
        self.click("garbage:99")
        self.click("mv:9999")


if __name__ == "__main__":
    unittest.main(verbosity=2)
