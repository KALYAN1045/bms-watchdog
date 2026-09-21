#!/usr/bin/env python3
"""BookMyShow ticket watchdog -- CLI entry point.

  python watch.py bot                         run the bot + watcher (recommended)
  python watch.py telegram-setup              connect Telegram, find your chat id
  python watch.py doctor                      check the setup end to end
  python watch.py movies --query paradise     find a movie's event code
  python watch.py theatres --event ET00436621 list exact theatre names
  python watch.py check                       run one pass (cron / CI)
  python watch.py run                         poll forever (instant alerts)
  python watch.py test-alert                  prove notifications work
"""

from __future__ import annotations

import argparse
import os
import random
import signal
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List

import warnings

warnings.filterwarnings("ignore", message=".*OpenSSL.*")

from bmswatch import config as configmod
from bmswatch.bms import parse_showtimes
from bmswatch.engine import check_watch, handle_result
from bmswatch.fetcher import FetchError, make_session
from bmswatch.bot import Bot
from bmswatch.notify import Notifier
from bmswatch.state import State
from bmswatch.store import WatchStore

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "watchlist.yaml"

_stop = False


def _handle_signal(signum, frame):
    global _stop
    _stop = True
    print("\nStopping after the current check…", flush=True)


def _load(args) -> configmod.Settings:
    try:
        return configmod.load(args.config)
    except configmod.ConfigError as exc:
        sys.exit(f"Config error: {exc}")


class SessionPool:
    """Keeps one Chrome per (city, region) alive across polls.

    Launching Chrome costs several seconds; a poll costs a fraction of one.
    A session that errors is dropped so the next pass starts a fresh browser.
    """

    def __init__(self, settings: configmod.Settings, verbose: bool):
        self.settings = settings
        self.verbose = verbose
        self._sessions = {}

    def get(self, city: str, region: str) -> BMSSession:
        key = (city, region)
        if key not in self._sessions:
            session = _session(self.settings, city, region, self.verbose)
            session.start()
            self._sessions[key] = session
        return self._sessions[key]

    def drop(self, city: str, region: str) -> None:
        session = self._sessions.pop((city, region), None)
        if session:
            session.close()

    def close(self) -> None:
        for session in list(self._sessions.values()):
            session.close()
        self._sessions.clear()


def _session(settings: configmod.Settings, city: str, region: str, verbose: bool):
    return make_session(
        engine=settings.engine,
        city=city,
        region_code=region,
        profile_dir=ROOT / settings.profile_dir,
        channel=settings.channel,
        window=settings.window,
        proxy=settings.proxy,
        verbose=verbose,
    )


def _standalone_session(args):
    """For the lookup commands, which don't read a watchlist."""
    return make_session(
        engine=os.environ.get("BMS_ENGINE", "auto"),
        city=args.city,
        region_code=args.region,
        profile_dir=ROOT / ".chrome-profile",
        verbose=args.verbose,
    )


# -- commands --------------------------------------------------------------

def cmd_movies(args) -> int:
    with _standalone_session(args) as s:
        movies = s.list_movies()
    query = (args.query or "").lower().replace(" ", "-")
    rows = sorted((slug, code) for code, slug in movies.items()
                  if not query or query in slug)
    if not rows:
        print(f"No movie in {args.city} matched {args.query!r}. "
              "Try a shorter query, or it may not be listed yet.")
        return 1
    print(f"{len(rows)} match(es) in {args.city}:\n")
    for slug, code in rows:
        print(f"  {code}   {slug}")
    print("\nPut the ET-code in watchlist.yaml as 'event_code'.")
    return 0


def cmd_theatres(args) -> int:
    date_code = args.date or datetime.now().strftime("%Y%m%d")
    date_code = configmod.date_code(date_code)
    with _standalone_session(args) as s:
        snap = parse_showtimes(s.showtimes(args.event, date_code), args.event, date_code)
        if not snap.shows and snap.open_dates:
            date_code = snap.open_dates[0]
            print(f"(no shows on the date you asked for; using {date_code})")
            snap = parse_showtimes(s.showtimes(args.event, date_code), args.event, date_code)

    if not snap.shows:
        print(f"{args.event}: booking is not open yet "
              f"(bookable dates: {', '.join(snap.open_dates) or 'none'})")
        return 1

    print(f"{snap.title} — {date_code}\n")
    venues = {}
    for show in snap.shows:
        venues.setdefault(show.venue, set()).add(f"{show.fmt} {show.attributes}".strip())
    for venue in sorted(venues):
        formats = ", ".join(sorted(f for f in venues[venue] if f))
        print(f"  {venue}" + (f"    [{formats}]" if formats else ""))
    print("\nCopy any substring of a name into 'theatres' in watchlist.yaml.")
    return 0


def cmd_test_alert(args) -> int:
    settings = _load(args)
    notifier = Notifier(settings.telegram, settings.desktop, verbose=True)
    if not notifier.channels:
        sys.exit("No notification channel configured. Set TELEGRAM_BOT_TOKEN and "
                 "TELEGRAM_CHAT_ID (see README), or enable desktop alerts.")
    print(f"Sending a test alert via: {', '.join(notifier.channels)}")
    notifier.repeat_count = 2
    notifier.repeat_every = 10
    notifier.alert(
        "test",
        "🎟 Test alert",
        "🚨 <b>TICKETS OPEN</b> — Test Movie\n\n📅 <b>Today</b>\n📍 <b>Prasads Multiplex: Hyderabad</b>\n"
        "   • 08:00 AM · IMAX 2D · Telugu — ₹250\n\n<i>This is a test. Tap ✅ Got it to stop.</i>",
        "TICKETS OPEN — Test Movie (this is a test)",
        "https://in.bookmyshow.com/explore/movies-hyderabad",
    )
    print("Done. If nothing arrived, run: python watch.py doctor")
    return 0


BOTFATHER_STEPS = """
Telegram bot setup — three steps, about two minutes:

  1. In Telegram, open a chat with  @BotFather
  2. Send  /newbot  and follow the prompts (any name, username must end in 'bot').
     It replies with a token that looks like  8012345678:AAE...
  3. Put that token in the .env file in this folder:

         TELEGRAM_BOT_TOKEN=8012345678:AAE...

     (No quotes, no 'export'. Create the file from .env.example if it's missing.)

Then run this command again — it finds your chat id and tests the alert for you.
The token stays in that file; nothing prints it.
"""


def _write_env_value(env_path: Path, key: str, value: str) -> None:
    """Set key=value in .env, leaving every other line untouched."""
    lines = env_path.read_text().splitlines() if env_path.exists() else []
    out, replaced = [], False
    for line in lines:
        if line.strip().startswith(f"{key}=") or line.strip().startswith(f"export {key}="):
            out.append(f"{key}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{key}={value}")
    env_path.write_text("\n".join(out).rstrip() + "\n")


def cmd_telegram_setup(args) -> int:
    from bmswatch.notify import Telegram, redact
    from bmswatch.config import TelegramConfig

    env_path = ROOT / ".env"
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token and env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip().removeprefix("export ").strip()
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                token = line.split("=", 1)[1].strip().strip("\"'")

    if not token:
        print(BOTFATHER_STEPS)
        return 1

    tg = Telegram(TelegramConfig(bot_token=token, chat_id=""))
    try:
        me = tg._call("getMe")
    except Exception as exc:
        print(f"That token was rejected: {redact(exc, token)}")
        print("Check you copied the whole thing from BotFather, including the part "
              "before the colon.")
        return 1

    username = me.get("username", "?")
    print(f"Bot is live: @{username}")

    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if chat_id and not args.force:
        print(f"Already have chat id {chat_id} — skipping discovery (use --force to redo).")
    else:
        print(f"\nNow open Telegram, go to  @{username},  and press Start "
              "(or send it any message).")
        print(f"Waiting up to {args.wait}s for that message…", flush=True)

        deadline = time.time() + args.wait
        chat_id = ""
        offset = None
        while time.time() < deadline and not chat_id:
            try:
                kwargs = {"timeout": 20}
                if offset is not None:
                    kwargs["offset"] = offset
                updates = tg._call("getUpdates", **kwargs) or []
            except Exception as exc:
                print(f"  …{redact(exc, token)}", flush=True)
                time.sleep(3)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                message = update.get("message") or update.get("edited_message") or {}
                chat = message.get("chat") or {}
                if chat.get("id"):
                    chat_id = str(chat["id"])
                    who = chat.get("first_name") or chat.get("username") or "you"
                    print(f"Got it — message from {who}, chat id {chat_id}")
                    break

        if not chat_id:
            print("\nNo message arrived. A bot can't message you until you message "
                  "it first, so that Start really is required.")
            print(f"Open Telegram → search @{username} → Start, then run this again.")
            return 1

    _write_env_value(env_path, "TELEGRAM_BOT_TOKEN", token)
    _write_env_value(env_path, "TELEGRAM_CHAT_ID", chat_id)
    print(f"Saved both values to {env_path.name}")

    tg.cfg.chat_id = chat_id
    tg.send("✅ <b>bms-watchdog is connected.</b>\n\nThis is where your ticket "
            "alerts will arrive.", silent=False)
    print("Sent a confirmation message — check Telegram.")
    print("\nNext:  source .env && ./venv/bin/python watch.py test-alert")
    return 0


def cmd_doctor(args) -> int:
    ok = True
    print("1. config")
    try:
        settings = configmod.load(args.config)
        print(f"   ok — {len(settings.watches)} watch(es), poll every {settings.poll_seconds}s")
    except configmod.ConfigError as exc:
        print(f"   FAIL — {exc}")
        return 1

    print("2. notifications")
    notifier = Notifier(settings.telegram, settings.desktop)
    if notifier.telegram:
        try:
            me = notifier.telegram._call("getMe")
            print(f"   ok — telegram bot @{me.get('username')}")
            notifier.telegram.send("✅ bms-watchdog doctor: Telegram is wired up.", silent=True)
            print(f"   ok — test message sent to chat {settings.telegram.chat_id}")
        except Exception as exc:
            ok = False
            print(f"   FAIL — telegram: {exc}")
    else:
        print("   -- telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")
    print(f"   {'ok' if notifier.desktop.available else '--'} — macOS desktop alerts "
          f"{'enabled' if notifier.desktop.available else 'unavailable'}")
    if not notifier.channels:
        ok = False
        print("   FAIL — no channel would deliver an alert")

    print("3. bookmyshow")
    watch = settings.watches[0]
    try:
        with _session(settings, watch.city, watch.region_code, args.verbose) as s:
            movies = s.list_movies()
            how = {"http": "plain HTTP (no browser needed)",
                   "browser": "headful browser"}.get(s.name, s.name)
            print(f"   ok — reaching BookMyShow via {how}")
            print(f"   ok — {len(movies)} movies listed in {watch.city}")
            state = State(ROOT / settings.state_file)
            result = check_watch(s, watch, state)
            print(f"   ok — '{watch.id}': {result.summary}")
            if result.error:
                ok = False
                print(f"   FAIL — {result.error}")
    except Exception as exc:
        ok = False
        print(f"   FAIL — {exc}")
        print("   hint: check your internet connection. If it says Cloudflare "
              "blocked you, set  engine: browser  in watchlist.yaml defaults.")

    print("\n" + ("All good. Start it with:  python watch.py run" if ok
                  else "Fix the FAILs above, then re-run doctor."))
    return 0 if ok else 1


def _run_pass(settings, state, notifier, verbose, pool: "SessionPool",
              alert_sink=None) -> None:
    by_region = {}
    for watch in settings.watches:
        if watch.enabled:
            by_region.setdefault((watch.city, watch.region_code), []).append(watch)

    for (city, region), watches in by_region.items():
        try:
            session = pool.get(city, region)
        except Exception as exc:                        # browser refused to start
            print(f"{datetime.now():%H:%M:%S} ⚠️  browser: {exc}", flush=True)
            continue

        for watch in watches:
            if _stop:
                return
            try:
                result = check_watch(session, watch, state)
            except Exception as exc:                    # never let one watch kill the loop
                print(f"{datetime.now():%H:%M:%S} ⚠️  {watch.id}: {exc}", flush=True)
                pool.drop(city, region)                 # start a clean browser next pass
                break
            handle_result(watch, result, state, notifier, verbose=True,
                          alert_sink=alert_sink)
            if result.status == "error":
                pool.drop(city, region)
                break


def cmd_check(args) -> int:
    """One pass, for cron or CI.

    Bot commands queued since the last run are handled first, so the Telegram
    interface still works without a long-running process -- at the cost of a
    reply only arriving on the next scheduled run.
    """
    settings = _load(args)
    notifier = Notifier(settings.telegram, settings.desktop, verbose=args.verbose)

    if notifier.telegram:
        store = WatchStore(ROOT / settings.store_path)
        changed = {"yes": False}

        def factory(city: str, region: str):
            return make_session(
                engine=settings.engine, city=city, region_code=region,
                profile_dir=ROOT / settings.profile_dir, channel=settings.channel,
                window=settings.window, proxy=settings.proxy, verbose=args.verbose)

        bot = Bot(notifier.telegram, store, factory, ROOT / ".cache",
                  on_change=lambda: changed.__setitem__("yes", True),
                  verbose=args.verbose)
        handled = 0
        for _ in range(12):                 # drain the queue, don't loop forever
            n = bot.poll(timeout=0)
            handled += n
            if not n:
                break
        if handled:
            print(f"handled {handled} bot update(s)", flush=True)
        if changed["yes"]:
            settings = _load(args)          # a watch was added or removed

    state = State(ROOT / settings.state_file)
    pool = SessionPool(settings, args.verbose)
    try:
        _run_pass(settings, state, notifier, args.verbose, pool)
    finally:
        pool.close()
        state.save()
    return 0


@dataclass
class PendingAlert:
    """An alert mid-way through its repeat schedule."""
    watch_id: str
    chat_id: str
    title: str
    body: str
    plain: str
    url: str
    rounds: int
    sent: int = 0
    next_at: float = 0.0


def cmd_bot(args) -> int:
    """Long-running: serve the Telegram bot and poll BookMyShow in one loop."""
    settings = _load(args)
    if not settings.telegram.configured:
        sys.exit("Telegram isn't configured. Run:  python watch.py telegram-setup")

    store = WatchStore(ROOT / settings.store_path)
    notifier = Notifier(settings.telegram, settings.desktop, verbose=args.verbose)
    telegram = notifier.telegram
    telegram.drain()                     # ignore anything sent before we started

    state = State(ROOT / settings.state_file)
    pool = SessionPool(settings, args.verbose)
    dirty = {"config": False}

    def factory(city: str, region: str):
        return make_session(
            engine=settings.engine, city=city, region_code=region,
            profile_dir=ROOT / settings.profile_dir, channel=settings.channel,
            window=settings.window, proxy=settings.proxy, verbose=args.verbose)

    bot = Bot(telegram, store, factory, ROOT / ".cache",
              on_change=lambda: dirty.__setitem__("config", True),
              verbose=args.verbose)

    pending: List[PendingAlert] = []

    def sink(watch, title, body, plain, url) -> None:
        pending.append(PendingAlert(
            watch_id=watch.id, chat_id=watch.chat_id or settings.telegram.chat_id,
            title=title, body=body, plain=plain, url=url,
            rounds=max(1, settings.telegram.repeat_count)))

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    print(f"Bot running as part of the watchdog. "
          f"{len(settings.watches)} watch(es), checking every {settings.poll_seconds}s.")
    print("Open Telegram and send /start")
    notifier.info("🤖 Watchdog online. Send /start to set up alerts.", silent=True)

    last_check = 0.0
    while not _stop:
        try:
            bot.poll(timeout=5)
        except Exception as exc:
            print(f"{datetime.now():%H:%M:%S} ⚠️  bot poll failed: {exc}", flush=True)
            time.sleep(2)

        # anything the user acknowledged stops repeating immediately
        if bot.acked:
            pending = [p for p in pending if p.watch_id not in bot.acked]

        now = time.time()
        still: List[PendingAlert] = []
        for alert in pending:
            if now >= alert.next_at:
                alert.sent += 1
                try:
                    notifier.alert_once(alert.watch_id, alert.title, alert.body,
                                        alert.plain, alert.url, round_no=alert.sent,
                                        rounds=alert.rounds, chat_id=alert.chat_id)
                except Exception as exc:      # never let one alert kill the loop
                    print(f"{datetime.now():%H:%M:%S} ⚠️  alert "
                          f"'{alert.watch_id}' failed: {exc}", flush=True)
                alert.next_at = now + settings.telegram.repeat_every_seconds
            if alert.sent < alert.rounds:
                still.append(alert)
        pending = still

        if dirty["config"] or now - last_check >= settings.poll_seconds:
            if dirty["config"]:
                dirty["config"] = False
                try:
                    settings = _load(args)       # pick up watches the bot just added
                except SystemExit:
                    pass
            _run_pass(settings, state, notifier, args.verbose, pool, alert_sink=sink)
            state.save()
            last_check = time.time()

    pool.close()
    notifier.info("🛑 Watchdog stopped.", silent=True)
    return 0


def cmd_run(args) -> int:
    settings = _load(args)
    state = State(ROOT / settings.state_file)
    notifier = Notifier(settings.telegram, settings.desktop, verbose=args.verbose)
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    active = [w.id for w in settings.watches if w.enabled]
    print(f"Watching {len(active)} movie(s): {', '.join(active)}")
    print(f"Every {settings.poll_seconds}s · alerts via {', '.join(notifier.channels) or 'nothing!'}")
    notifier.info(f"👀 Watchdog started — {', '.join(active)}", silent=True)

    pool = SessionPool(settings, args.verbose)
    last_heartbeat = time.time()
    try:
        while not _stop:
            _run_pass(settings, state, notifier, args.verbose, pool)
            state.save()

            if settings.heartbeat_minutes and \
                    time.time() - last_heartbeat > settings.heartbeat_minutes * 60:
                notifier.info(
                    f"💤 Still watching ({datetime.now():%d %b %H:%M}) — nothing open yet.",
                    silent=True)
                last_heartbeat = time.time()

            nap = settings.poll_seconds + random.uniform(0, settings.jitter_seconds)
            waited = 0.0
            while waited < nap and not _stop:
                time.sleep(min(1.0, nap - waited))
                waited += 1.0
    finally:
        pool.close()

    notifier.info("🛑 Watchdog stopped.", silent=True)
    return 0


def main() -> int:
    # shared flags, accepted both before and after the subcommand
    # SUPPRESS so a flag given before the subcommand isn't reset by the subparser
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", default=argparse.SUPPRESS,
                        help="watchlist file (default: watchlist.yaml)")
    common.add_argument("-v", "--verbose", action="store_true",
                        default=argparse.SUPPRESS)

    parser = argparse.ArgumentParser(
        prog="watch.py", parents=[common],
        description="Alert me the moment BookMyShow opens tickets.")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, help_text):
        return sub.add_parser(name, help=help_text, parents=[common])

    add("doctor", "check config, notifications and BookMyShow access")
    add("check", "run one pass and exit").set_defaults(func=cmd_check)
    add("run", "poll continuously").set_defaults(func=cmd_run)
    add("test-alert", "send a fake alert").set_defaults(func=cmd_test_alert)
    add("bot", "run the Telegram bot and the watcher together"
        ).set_defaults(func=cmd_bot)

    p_tg = add("telegram-setup", "connect a Telegram bot and find your chat id")
    p_tg.add_argument("--wait", type=int, default=180,
                      help="seconds to wait for your first message (default 180)")
    p_tg.add_argument("--force", action="store_true",
                      help="re-discover the chat id even if one is already set")
    p_tg.set_defaults(func=cmd_telegram_setup)

    p_movies = add("movies", "find a movie's ET-code")
    p_movies.add_argument("-q", "--query", default="")
    p_movies.add_argument("--city", default="hyderabad")
    p_movies.add_argument("--region", default="HYD")
    p_movies.set_defaults(func=cmd_movies)

    p_th = add("theatres", "list theatre names showing a movie")
    p_th.add_argument("-e", "--event", required=True, help="ET-code, e.g. ET00436621")
    p_th.add_argument("-d", "--date", default=None, help="YYYY-MM-DD (default: today)")
    p_th.add_argument("--city", default="hyderabad")
    p_th.add_argument("--region", default="HYD")
    p_th.set_defaults(func=cmd_theatres)

    args = parser.parse_args()
    args.config = getattr(args, "config", None) or str(DEFAULT_CONFIG)
    args.verbose = getattr(args, "verbose", False)
    if args.command == "doctor":
        return cmd_doctor(args)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
