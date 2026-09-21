"""The Telegram side: set a city, browse what's showing, create alerts.

Everything a person needs to do is a button press. Free text is only ever
asked for when searching, and even then the reply is a list of buttons.
"""

from __future__ import annotations

import html
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from .bms import parse_showtimes
from .catalogue import (Movie, Region, load_movies, load_regions, nearest_regions,
                        search_regions, top_regions)
from .config import SLOT_ORDER, TIME_SLOTS, Watch, describe_slots
from .matcher import filter_shows
from .fetcher import FetchError
from .store import WatchStore

PAGE = 6                      # list rows per screen; Telegram gets cramped past this
MAX_PICK = 12                 # theatres or dates in one watch


def esc(text: Any) -> str:
    return html.escape(str(text), quote=False)


def pretty_date(code: str) -> str:
    try:
        return datetime.strptime(code, "%Y%m%d").strftime("%a %d %b")
    except ValueError:
        return code


@dataclass
class Session:
    """What one person is part-way through choosing."""
    step: str = "idle"
    awaiting: str = ""                       # "city" | "movie" -- expecting typed text
    page: int = 0
    regions: List[Region] = field(default_factory=list)
    region_hits: List[Region] = field(default_factory=list)
    movies: List[Movie] = field(default_factory=list)
    movie: Optional[Movie] = None
    venues: List[str] = field(default_factory=list)
    shows_by_date: Dict[str, list] = field(default_factory=dict)
    theatres: List[str] = field(default_factory=list)
    dates_available: List[str] = field(default_factory=list)
    dates: List[str] = field(default_factory=list)
    slots: List[str] = field(default_factory=list)
    booking_open: bool = False
    touched: float = field(default_factory=time.time)

    def reset(self) -> None:
        self.__init__()                      # type: ignore[misc]


class Bot:
    """Stateless-ish command handling over a Telegram long poll."""

    def __init__(self, telegram, store: WatchStore, session_factory: Callable,
                 cache_dir, on_change: Optional[Callable] = None,
                 verbose: bool = False):
        self.tg = telegram
        self.store = store
        self.session_factory = session_factory
        self.cache_dir = cache_dir
        self.on_change = on_change or (lambda: None)
        self.verbose = verbose
        self.sessions: Dict[str, Session] = {}
        self.acked: set = set()            # watch ids the user has said "got it" to
        # pick up wherever the Telegram client already is, so a drained
        # backlog isn't replayed as commands on startup
        self._offset: Optional[int] = getattr(telegram, "_offset", None)

    # -- plumbing ----------------------------------------------------------

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"[bot] {msg}", flush=True)

    def session(self, chat_id: str) -> Session:
        s = self.sessions.get(chat_id)
        if s is None or time.time() - s.touched > 3600:
            s = Session()
            self.sessions[chat_id] = s
        s.touched = time.time()
        return s

    def poll(self, timeout: int = 5) -> int:
        """Handle whatever is waiting. Returns how many updates were processed."""
        try:
            updates = self.tg.get_updates(offset=self._offset, timeout=timeout,
                                          allowed=["message", "callback_query"])
        except Exception as exc:
            self._log(f"getUpdates failed: {exc}")
            return 0
        for update in updates:
            self._offset = update["update_id"] + 1
            try:
                self._dispatch(update)
            except Exception as exc:            # a bad button must not kill the bot
                self._log(f"handler error: {exc}")
        return len(updates)

    def _dispatch(self, update: dict) -> None:
        if "callback_query" in update:
            cb = update["callback_query"]
            chat_id = str(cb["message"]["chat"]["id"])
            self.tg.answer_callback(cb["id"])
            self._on_callback(chat_id, cb["message"]["message_id"], cb.get("data", ""))
        elif "message" in update:
            msg = update["message"]
            chat_id = str(msg["chat"]["id"])
            if msg.get("location"):
                self._on_location(chat_id, msg["location"])
            elif msg.get("text"):
                self._on_text(chat_id, msg["text"].strip())

    def _say(self, chat_id: str, text: str, keyboard=None, reply_markup=None):
        return self.tg.send(text, keyboard=keyboard, reply_markup=reply_markup,
                            chat_id=chat_id, silent=True)

    def _edit(self, chat_id: str, message_id: int, text: str, keyboard=None):
        try:
            self.tg.edit(chat_id, message_id, text, keyboard=keyboard)
        except Exception:
            self._say(chat_id, text, keyboard=keyboard)

    # -- regions -----------------------------------------------------------

    def _regions(self, sess: Session) -> List[Region]:
        if not sess.regions:
            fetcher = self.session_factory("hyderabad", "HYD")
            try:
                fetcher.start()
                sess.regions = load_regions(
                    fetcher, cache_path=self.cache_dir / "regions.json")
            finally:
                fetcher.close()
        return sess.regions

    def _city(self, chat_id: str) -> Optional[dict]:
        user = self.store.get_user(chat_id)
        return user if user.get("region_code") else None

    # -- entry points ------------------------------------------------------

    def _on_text(self, chat_id: str, text: str) -> None:
        sess = self.session(chat_id)
        low = text.lower()

        if low in ("/start", "start", "/menu"):
            sess.reset()
            return self._main_menu(chat_id)
        if low in ("/help", "help"):
            return self._help(chat_id)
        if low in ("/cancel", "cancel", "/stop"):
            sess.reset()
            return self._say(chat_id, "Cancelled.", keyboard=self._menu_keyboard())
        if low in ("/list", "/alerts"):
            return self._show_list(chat_id)
        if low in ("/city", "/location"):
            return self._ask_city(chat_id)
        if low in ("/add", "/new"):
            return self._start_add(chat_id)

        if sess.awaiting == "city":
            return self._city_results(chat_id, text)
        if sess.awaiting == "movie":
            return self._movie_search(chat_id, text)

        # anything else: treat as an acknowledgement of an alert, or a nudge
        if low in ("ok", "okay", "got it", "ack", "thanks"):
            return
        self._say(chat_id, "Not sure what that means — here's the menu.",
                  keyboard=self._menu_keyboard())

    def _on_location(self, chat_id: str, location: dict) -> None:
        sess = self.session(chat_id)
        lat, lon = location.get("latitude"), location.get("longitude")
        self._say(chat_id, "📍 Finding your nearest city…")
        try:
            regions = self._regions(sess)
        except Exception as exc:
            return self._say(chat_id, f"Couldn't load the city list: {esc(exc)}")
        near = nearest_regions(regions, float(lat), float(lon), limit=4)
        sess.region_hits = [r for r, _ in near]
        sess.awaiting = ""
        rows = [[{"text": f"{r.name} · {km:.0f} km", "callback_data": f"city:{i}"}]
                for i, (r, km) in enumerate(near)]
        rows.append([{"text": "🔍 Somewhere else", "callback_data": "city:search"}])
        self._say(chat_id, "Closest BookMyShow cities to you — pick one:",
                  keyboard=rows, reply_markup=None)

    # -- menus -------------------------------------------------------------

    def _menu_keyboard(self) -> List[List[dict]]:
        return [
            [{"text": "➕ New alert", "callback_data": "add"}],
            [{"text": "📋 My alerts", "callback_data": "list"}],
            [{"text": "📍 Change city", "callback_data": "city:ask"}],
        ]

    def _main_menu(self, chat_id: str) -> None:
        city = self._city(chat_id)
        n = len(self.store.watches(chat_id))
        where = f"📍 <b>{esc(city['city_name'])}</b>" if city else "📍 <i>no city set yet</i>"
        self._say(
            chat_id,
            "🎟 <b>BookMyShow watchdog</b>\n\n"
            f"{where}\n"
            f"🔔 {n} active alert{'s' if n != 1 else ''}\n\n"
            "I check BookMyShow every minute and ping you the moment tickets "
            "open for the movies and theatres you pick.",
            keyboard=self._menu_keyboard())
        if not city:
            self._ask_city(chat_id)

    def _help(self, chat_id: str) -> None:
        self._say(chat_id,
                  "<b>Commands</b>\n"
                  "/add — create an alert\n"
                  "/list — see and remove your alerts\n"
                  "/city — change your city\n"
                  "/cancel — abandon what you're doing\n\n"
                  "When tickets open I'll ping you repeatedly until you tap "
                  "<b>✅ Got it</b>.",
                  keyboard=self._menu_keyboard())

    # -- choosing a city ---------------------------------------------------

    def _ask_city(self, chat_id: str) -> None:
        sess = self.session(chat_id)
        sess.awaiting = "city"
        self._say(
            chat_id,
            "Which city are you booking in?\n\n"
            "Tap the button to share your location, or just type a city name.",
            reply_markup={
                "keyboard": [[{"text": "📍 Share my location", "request_location": True}]],
                "resize_keyboard": True, "one_time_keyboard": True,
            })

    def _city_results(self, chat_id: str, query: str) -> None:
        sess = self.session(chat_id)
        try:
            regions = self._regions(sess)
        except Exception as exc:
            return self._say(chat_id, f"Couldn't load the city list: {esc(exc)}")
        hits = search_regions(regions, query, limit=8) or top_regions(regions)
        sess.region_hits = hits
        note = ("No city matched that — here are the big ones:"
                if not search_regions(regions, query, limit=1) else "Pick your city:")
        rows = [[{"text": r.label, "callback_data": f"city:{i}"}]
                for i, r in enumerate(hits)]
        self._say(chat_id, note, keyboard=rows,
                  reply_markup=None if rows else {"remove_keyboard": True})

    def _set_city(self, chat_id: str, message_id: int, index: int) -> None:
        sess = self.session(chat_id)
        if index >= len(sess.region_hits):
            return self._ask_city(chat_id)
        region = sess.region_hits[index]
        self.store.set_city(chat_id, name=region.name, code=region.code, slug=region.slug)
        sess.awaiting = ""
        sess.movies = []
        self._edit(chat_id, message_id, f"📍 City set to <b>{esc(region.name)}</b>.")
        self._start_add(chat_id)

    # -- choosing a movie --------------------------------------------------

    def _start_add(self, chat_id: str) -> None:
        city = self._city(chat_id)
        if not city:
            return self._ask_city(chat_id)
        sess = self.session(chat_id)
        sess.step = "movie"
        sess.page = 0
        sess.movie = None
        sess.theatres = []
        sess.dates = []
        sess.slots = []
        if not sess.movies:
            self._say(chat_id, f"Loading what's on in {esc(city['city_name'])}…")
            fetcher = self.session_factory(city["city"], city["region_code"])
            try:
                fetcher.start()
                sess.movies = load_movies(fetcher, city["city"])
            except Exception as exc:
                return self._say(chat_id, f"Couldn't load movies: {esc(exc)}")
            finally:
                fetcher.close()
        if not sess.movies:
            return self._say(chat_id, "BookMyShow lists nothing in that city right now.")
        self._show_movies(chat_id, None)

    def _movie_rows(self, sess: Session) -> Tuple[str, List[List[dict]]]:
        total = len(sess.movies)
        pages = max(1, (total + PAGE - 1) // PAGE)
        sess.page = max(0, min(sess.page, pages - 1))
        start = sess.page * PAGE
        chunk = sess.movies[start:start + PAGE]
        rows = [[{"text": f"{'🔜' if m.upcoming else '🎬'} {m.label[:52]}",
                  "callback_data": f"mv:{start + i}"}]
                for i, m in enumerate(chunk)]
        nav = []
        if sess.page > 0:
            nav.append({"text": "◀", "callback_data": f"mvp:{sess.page - 1}"})
        nav.append({"text": f"{sess.page + 1}/{pages}", "callback_data": "noop"})
        if sess.page < pages - 1:
            nav.append({"text": "▶", "callback_data": f"mvp:{sess.page + 1}"})
        rows.append(nav)
        rows.append([{"text": "🔍 Search by name", "callback_data": "mv:search"}])
        text = (f"<b>Pick a movie</b>  ({total} listed)\n"
                "🎬 now showing · 🔜 coming soon")
        return text, rows

    def _show_movies(self, chat_id: str, message_id: Optional[int]) -> None:
        sess = self.session(chat_id)
        text, rows = self._movie_rows(sess)
        if message_id:
            self._edit(chat_id, message_id, text, keyboard=rows)
        else:
            self._say(chat_id, text, keyboard=rows)

    def _movie_search(self, chat_id: str, query: str) -> None:
        sess = self.session(chat_id)
        sess.awaiting = ""
        q = query.lower()
        hits = [m for m in sess.movies if q in m.label.lower() or q in m.slug]
        if not hits:
            return self._say(chat_id, f"Nothing matching “{esc(query)}”. "
                                      "It may not be listed yet — try /add and browse.")
        rows = [[{"text": f"{'🔜' if m.upcoming else '🎬'} {m.label[:52]}",
                  "callback_data": f"mv:{sess.movies.index(m)}"}] for m in hits[:8]]
        self._say(chat_id, f"Matches for “{esc(query)}”:", keyboard=rows)

    # -- choosing theatres -------------------------------------------------

    def _pick_movie(self, chat_id: str, message_id: int, index: int) -> None:
        sess = self.session(chat_id)
        if index >= len(sess.movies):
            return self._show_movies(chat_id, message_id)
        movie = sess.movies[index]
        sess.movie = movie
        sess.step = "dates"
        sess.page = 0
        sess.theatres = []
        sess.dates = []
        sess.slots = []
        sess.venues = []
        sess.shows_by_date = {}
        city = self._city(chat_id)

        self._edit(chat_id, message_id,
                   f"🎬 <b>{esc(movie.label)}</b>\n\nChecking what's on sale…")

        dates: List[str] = []
        fetcher = self.session_factory(city["city"], city["region_code"])
        try:
            fetcher.start()
            today = datetime.now().strftime("%Y%m%d")
            probe = parse_showtimes(fetcher.showtimes(movie.code, today),
                                    movie.code, today)
            dates = probe.open_dates[:8]
        except FetchError as exc:
            self._log(f"date probe failed: {exc}")
        finally:
            fetcher.close()

        sess.dates_available = dates
        sess.booking_open = bool(dates)
        text, rows = self._date_rows(sess)
        self._edit(chat_id, message_id, text, keyboard=rows)

    def _load_venues(self, chat_id: str, message_id: int) -> None:
        """Theatres are listed for the dates actually chosen, not a guess."""
        sess = self.session(chat_id)
        city = self._city(chat_id)
        movie = sess.movie
        sess.step = "theatres"
        sess.page = 0
        sess.theatres = []

        wanted = sess.dates or sess.dates_available[:5]
        if not wanted:
            sess.venues = []
            sess.shows_by_date = {}
            return self._show_theatres(chat_id, message_id)

        self._edit(chat_id, message_id,
                   f"🎬 <b>{esc(movie.label)}</b>\n\n"
                   f"Checking which theatres have it on "
                   f"{esc(', '.join(pretty_date(d) for d in wanted))}…")

        shows: Dict[str, list] = {}
        fetcher = self.session_factory(city["city"], city["region_code"])
        try:
            fetcher.start()
            for code in wanted:
                snap = parse_showtimes(fetcher.showtimes(movie.code, code),
                                       movie.code, code)
                shows[code] = snap.shows
        except FetchError as exc:
            self._log(f"venue lookup failed: {exc}")
        finally:
            fetcher.close()

        sess.shows_by_date = shows
        sess.venues = sorted({x.venue for day in shows.values() for x in day})
        self._show_theatres(chat_id, message_id)

    def _theatre_rows(self, sess: Session) -> Tuple[str, List[List[dict]]]:
        movie = sess.movie.label if sess.movie else "?"
        when = ", ".join(pretty_date(d) for d in sess.dates) or "any date"
        if not sess.venues:
            text = (f"🎬 <b>{esc(movie)}</b>\n📅 {esc(when)}\n\n"
                    "No theatre has it on sale for that yet.\n"
                    "I'll watch <b>every theatre</b> in your city and tell you the "
                    "moment one opens.")
            return text, [[{"text": "✅ Watch all theatres", "callback_data": "th:all"}],
                          [{"text": "✖ Cancel", "callback_data": "x"}]]

        total = len(sess.venues)
        pages = max(1, (total + PAGE - 1) // PAGE)
        sess.page = max(0, min(sess.page, pages - 1))
        start = sess.page * PAGE
        rows = []
        for i, venue in enumerate(sess.venues[start:start + PAGE]):
            idx = start + i
            mark = "☑️" if venue in sess.theatres else "▫️"
            rows.append([{"text": f"{mark} {venue[:50]}", "callback_data": f"th:{idx}"}])
        nav = []
        if sess.page > 0:
            nav.append({"text": "◀", "callback_data": f"thp:{sess.page - 1}"})
        nav.append({"text": f"{sess.page + 1}/{pages}", "callback_data": "noop"})
        if sess.page < pages - 1:
            nav.append({"text": "▶", "callback_data": f"thp:{sess.page + 1}"})
        rows.append(nav)
        rows.append([{"text": "🏛 Any theatre", "callback_data": "th:all"},
                     {"text": f"✔ Done ({len(sess.theatres)})", "callback_data": "th:done"}])
        text = (f"🎬 <b>{esc(movie)}</b>\n📅 {esc(when)}\n\n"
                f"<b>Which theatres?</b> Tap to select, or choose <i>Any theatre</i>.\n"
                f"{total} showing it on those dates.")
        return text, rows

    def _show_theatres(self, chat_id: str, message_id: int) -> None:
        text, rows = self._theatre_rows(self.session(chat_id))
        self._edit(chat_id, message_id, text, keyboard=rows)

    # -- choosing dates ----------------------------------------------------

    def _date_rows(self, sess: Session) -> Tuple[str, List[List[dict]]]:
        movie = sess.movie.label if sess.movie else "?"
        rows = []
        for i, code in enumerate(sess.dates_available[:8]):
            mark = "☑️" if code in sess.dates else "▫️"
            rows.append([{"text": f"{mark} {pretty_date(code)}",
                          "callback_data": f"dt:{i}"}])
        rows.append([{"text": "📅 Any date", "callback_data": "dt:any"},
                     {"text": f"✔ Done ({len(sess.dates)})", "callback_data": "dt:done"}])
        hint = ("" if sess.dates_available else
                "\n\n<i>Nothing on sale yet — “Any date” is the one you want.</i>")
        text = (f"🎬 <b>{esc(movie)}</b>\n\n<b>Which dates?</b>{hint}")
        return text, rows

    def _time_rows(self, sess: Session) -> Tuple[str, List[List[dict]]]:
        movie = sess.movie.label if sess.movie else "?"
        when = ", ".join(pretty_date(d) for d in sess.dates) or "Any date"
        where = ", ".join(sess.theatres) or "Any theatre"
        rows = []
        for i, key in enumerate(SLOT_ORDER):
            slot = TIME_SLOTS[key]
            mark = "☑️" if key in sess.slots else "▫️"
            rows.append([{
                "text": f"{mark} {slot['icon']} {slot['label']}  ·  {slot['when']}",
                "callback_data": f"tm:{i}"}])
        rows.append([{"text": "🕐 Any time", "callback_data": "tm:any"},
                     {"text": f"✔ Done ({len(sess.slots)})", "callback_data": "tm:done"}])
        text = (f"🎬 <b>{esc(movie)}</b>\n🏛 {esc(where)}\n📅 {esc(when)}\n\n"
                "<b>Which show timings?</b>\n"
                "Pick as many as you like — I'll only alert for shows that start "
                "in those windows.")
        return text, rows

    def _show_times(self, chat_id: str, message_id: int) -> None:
        text, rows = self._time_rows(self.session(chat_id))
        self._edit(chat_id, message_id, text, keyboard=rows)

    # -- confirm and save --------------------------------------------------

    def _preview(self, sess: Session) -> str:
        """How many shows this alert matches right now.

        Zero is legitimate -- that is the whole point when booking hasn't
        opened -- but it must be said out loud, so a filter that can never
        match (a theatre that doesn't screen it on those dates, say) is
        obvious before the alert is created rather than as silence later.
        """
        if not sess.shows_by_date:
            return ("🔔 Nothing on sale for this yet — you'll be pinged the "
                    "moment it opens.")
        probe = Watch(
            id="preview", movie="preview",
            theatres=list(sess.theatres),
            slots=list(sess.slots),
            windows=[(TIME_SLOTS[k]["from"], TIME_SLOTS[k]["to"])
                     for k in sess.slots if k in TIME_SLOTS])
        dates = sess.dates or list(sess.shows_by_date)
        shows = [x for d in dates for x in sess.shows_by_date.get(d, [])]
        hits = filter_shows(probe, shows)
        if hits:
            free = sum(1 for h in hits if not h.sold_out)
            return (f"✅ <b>{len(hits)} show(s) already match</b> "
                    f"({free} with seats). You'll be pinged about new ones.")

        if not shows:
            return ("🔔 Nothing on sale for those dates yet — you'll be pinged "
                    "the moment it opens.")

        # narrow down which filter is the one excluding everything
        pool = shows
        if sess.theatres:
            pool = [x for x in shows
                    if any(t.lower() in x.venue.lower() for t in sess.theatres)]
            if not pool:
                return ("⚠️ <b>That theatre has no shows on those dates yet.</b>\n"
                        "Fine if you're waiting for it to add some — otherwise go "
                        "back and widen the dates or theatres.")
        if sess.slots:
            where = "that theatre has" if sess.theatres else "there are"
            return (f"⚠️ <b>No show matches your timings yet</b> — {where} "
                    f"{len(pool)} show(s) on those dates, just not in those "
                    "windows.")
        return "🔔 Nothing matches yet — you'll be pinged when something does."

    def _confirm(self, chat_id: str, message_id: int) -> None:
        sess = self.session(chat_id)
        city = self._city(chat_id)
        movie = sess.movie
        where = "\n".join(f"   • {esc(t)}" for t in sess.theatres) or "   • Any theatre"
        when = ", ".join(pretty_date(d) for d in sess.dates) or "Any date"
        times = describe_slots(sess.slots)
        self._edit(
            chat_id, message_id,
            "<b>Create this alert?</b>\n\n"
            f"🎬 {esc(movie.label if movie else '?')}\n"
            f"📍 {esc(city['city_name'])}\n"
            f"🏛 Theatres:\n{where}\n"
            f"📅 {esc(when)}\n"
            f"🕐 {esc(times)}\n\n"
            f"{self._preview(sess)}",
            keyboard=[[{"text": "✅ Create alert", "callback_data": "ok"}],
                      [{"text": "◀ Change dates", "callback_data": "back:dates"}],
                      [{"text": "✖ Cancel", "callback_data": "x"}]])

    def _save(self, chat_id: str, message_id: int) -> None:
        sess = self.session(chat_id)
        city = self._city(chat_id)
        movie = sess.movie
        if not movie or not city:
            return self._edit(chat_id, message_id, "That selection expired — try /add again.")

        existing = self.store.duplicate_of(chat_id, movie.code, sess.theatres,
                                           sess.dates, sess.slots)
        if existing:
            sess.reset()
            return self._edit(chat_id, message_id,
                              "You already have an identical alert — nothing added.",
                              keyboard=self._menu_keyboard())

        watch_id = self.store.add({
            "id": f"{movie.slug}-{city['region_code'].lower()}",
            "chat_id": chat_id,
            "movie": movie.label,
            "event_code": movie.code,
            "city": city["city"],
            "region_code": city["region_code"],
            "theatres": list(sess.theatres),
            "dates": list(sess.dates),
            "slots": list(sess.slots),
        })
        sess.reset()
        self.on_change()
        self._edit(chat_id, message_id,
                   f"✅ <b>Watching {esc(movie.label)}</b>\n\n"
                   f"Alert id <code>{esc(watch_id)}</code>. "
                   "Add as many as you like — /add for another, /list to manage them.",
                   keyboard=self._menu_keyboard())

    # -- listing and removing ----------------------------------------------

    def _show_list(self, chat_id: str, message_id: Optional[int] = None) -> None:
        watches = self.store.watches(chat_id)
        if not watches:
            text = "You have no alerts yet."
            rows = [[{"text": "➕ New alert", "callback_data": "add"}]]
        else:
            lines = ["<b>Your alerts</b>\n"]
            rows = []
            for w in watches:
                where = ", ".join(w.get("theatres") or []) or "any theatre"
                when = ", ".join(pretty_date(d) for d in (w.get("dates") or [])) or "any date"
                times = describe_slots(w.get("slots") or [])
                state = "" if w.get("enabled", True) else "  <i>(paused)</i>"
                lines.append(f"🎬 <b>{esc(w.get('movie'))}</b>{state}\n"
                             f"   🏛 {esc(where)}\n   📅 {esc(when)}\n"
                             f"   🕐 {esc(times)}")
                rows.append([{"text": f"🗑 {str(w.get('movie'))[:40]}",
                              "callback_data": f"rm:{w.get('id')}"}])
            rows.append([{"text": "➕ New alert", "callback_data": "add"}])
            text = "\n\n".join(lines)
        if message_id:
            self._edit(chat_id, message_id, text, keyboard=rows)
        else:
            self._say(chat_id, text, keyboard=rows)

    # -- callback routing --------------------------------------------------

    def _on_callback(self, chat_id: str, message_id: int, data: str) -> None:
        sess = self.session(chat_id)

        if data.startswith("ack:"):
            self.acked.add(data[4:])
            return self._say(chat_id, "🔕 Okay, I'll stop pinging about that one.")
        if data == "noop":
            return
        if data == "add":
            return self._start_add(chat_id)
        if data == "list":
            return self._show_list(chat_id, message_id)
        if data == "x":
            sess.reset()
            return self._edit(chat_id, message_id, "Cancelled.",
                              keyboard=self._menu_keyboard())
        if data == "ok":
            return self._save(chat_id, message_id)
        if data == "back:dates":
            sess.step = "dates"
            text, rows = self._date_rows(sess)
            return self._edit(chat_id, message_id, text, keyboard=rows)

        head, _, tail = data.partition(":")

        if head == "city":
            if tail in ("ask", "search"):
                return self._ask_city(chat_id)
            if tail.isdigit():
                return self._set_city(chat_id, message_id, int(tail))
        elif head == "mv":
            if tail == "search":
                sess.awaiting = "movie"
                return self._say(chat_id, "Type part of the movie name:")
            if tail.isdigit():
                return self._pick_movie(chat_id, message_id, int(tail))
        elif head == "mvp" and tail.isdigit():
            sess.page = int(tail)
            return self._show_movies(chat_id, message_id)
        elif head == "thp" and tail.isdigit():
            sess.page = int(tail)
            return self._show_theatres(chat_id, message_id)
        elif head == "th":
            if tail == "all":
                sess.theatres = []
                sess.step = "times"
                return self._show_times(chat_id, message_id)
            if tail == "done":
                sess.step = "times"
                return self._show_times(chat_id, message_id)
            if tail.isdigit() and int(tail) < len(sess.venues):
                venue = sess.venues[int(tail)]
                if venue in sess.theatres:
                    sess.theatres.remove(venue)
                elif len(sess.theatres) < MAX_PICK:
                    sess.theatres.append(venue)
                return self._show_theatres(chat_id, message_id)
        elif head == "dt":
            if tail == "any":
                sess.dates = []
                return self._load_venues(chat_id, message_id)
            if tail == "done":
                return self._load_venues(chat_id, message_id)
            if tail.isdigit() and int(tail) < len(sess.dates_available):
                code = sess.dates_available[int(tail)]
                if code in sess.dates:
                    sess.dates.remove(code)
                elif len(sess.dates) < MAX_PICK:
                    sess.dates.append(code)
                text, rows = self._date_rows(sess)
                return self._edit(chat_id, message_id, text, keyboard=rows)
        elif head == "tm":
            if tail == "any":
                sess.slots = []
                return self._confirm(chat_id, message_id)
            if tail == "done":
                return self._confirm(chat_id, message_id)
            if tail.isdigit() and int(tail) < len(SLOT_ORDER):
                key = SLOT_ORDER[int(tail)]
                if key in sess.slots:
                    sess.slots.remove(key)
                else:
                    sess.slots.append(key)
                return self._show_times(chat_id, message_id)
        elif head == "rm":
            removed = self.store.remove(tail)
            self.on_change()
            if removed:
                self._say(chat_id, "🗑 Alert removed.")
            return self._show_list(chat_id, message_id)

        self._log(f"unhandled callback {data!r}")
