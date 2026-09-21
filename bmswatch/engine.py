"""One check cycle: resolve the movie, pull showtimes, match, alert."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from .bms import Show, Snapshot, parse_showtimes
from .config import Watch
from .fetcher import BMSSession, FetchError
from .matcher import filter_shows
from .notify import Notifier, esc
from .state import State

MAX_SHOWS_IN_MESSAGE = 24


@dataclass
class CheckResult:
    watch_id: str
    status: str                      # not_listed | closed | no_match | open
    summary: str
    matched: List[Show] = field(default_factory=list)
    fresh: List[Show] = field(default_factory=list)
    snapshot: Optional[Snapshot] = None
    error: Optional[str] = None

    @property
    def should_alert(self) -> bool:
        return bool(self.fresh)


def _pretty_date(date_code: str) -> str:
    try:
        return datetime.strptime(date_code, "%Y%m%d").strftime("%a, %d %b %Y")
    except ValueError:
        return date_code


def _slugify(text: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def resolve_event_code(session: BMSSession, watch: Watch, state: State) -> Optional[str]:
    """Find the ET-code for a movie, remembering it once we have it."""
    if watch.event_code:
        return watch.event_code
    cached = state.event_code(watch.id)
    if cached:
        return cached

    target = _slugify(watch.search or watch.movie)
    words = [w for w in target.split("-") if len(w) > 2]
    best: Optional[str] = None
    for code, slug in session.list_movies().items():
        if slug == target:
            best = code
            break
        if words and all(w in slug for w in words):
            best = best or code
    if best:
        state.set_event_code(watch.id, best)
    return best


def _dates_for(session: BMSSession, watch: Watch, event_code: str) -> tuple[List[str], Snapshot]:
    """Which dates to poll, plus the probe snapshot we got them from."""
    probe_date = watch.dates[0] if watch.dates else datetime.now().strftime("%Y%m%d")
    payload = session.showtimes(event_code, probe_date)
    probe = parse_showtimes(payload, event_code, probe_date)
    return watch.resolved_dates(probe.open_dates), probe


def check_watch(session: BMSSession, watch: Watch, state: State) -> CheckResult:
    try:
        event_code = resolve_event_code(session, watch, state)
    except FetchError as exc:
        return CheckResult(watch.id, "error", "could not reach BookMyShow", error=str(exc))

    if not event_code:
        return CheckResult(watch.id, "not_listed",
                           f"{watch.movie}: not listed in {watch.city} yet")

    try:
        dates, probe = _dates_for(session, watch, event_code)
    except FetchError as exc:
        return CheckResult(watch.id, "error", f"{watch.movie}: fetch failed", error=str(exc))

    if not dates:
        return CheckResult(watch.id, "closed",
                           f"{watch.movie}: listed, booking not open yet", snapshot=probe)

    snapshot = probe
    all_shows: List[Show] = []
    for date_code in dates:
        if date_code == probe.requested_date:
            snap = probe
        else:
            try:
                snap = parse_showtimes(session.showtimes(event_code, date_code),
                                       event_code, date_code)
            except FetchError as exc:
                return CheckResult(watch.id, "error", f"{watch.movie}: fetch failed",
                                   error=str(exc), snapshot=snapshot)
        if snap.title and not snapshot.title:
            snapshot = snap          # keep whichever response actually named the film
        all_shows.extend(snap.shows)

    if not all_shows:
        return CheckResult(watch.id, "closed",
                           f"{watch.movie}: no shows on {', '.join(_pretty_date(d) for d in dates)}",
                           snapshot=snapshot)

    matched = filter_shows(watch, all_shows)
    if not matched:
        return CheckResult(
            watch.id, "no_match",
            f"{watch.movie}: {len(all_shows)} shows listed, none match your filters",
            snapshot=snapshot)

    fresh_ids = set(state.new_fingerprints(watch.id, [s.fingerprint for s in matched]))
    fresh = [s for s in matched if s.fingerprint in fresh_ids]
    return CheckResult(
        watch.id, "open",
        f"{watch.movie}: {len(matched)} matching shows ({len(fresh)} new)",
        matched=matched, fresh=fresh, snapshot=snapshot)


# -- message building ------------------------------------------------------

def _group(shows: List[Show]) -> Dict[str, Dict[str, List[Show]]]:
    grouped: Dict[str, Dict[str, List[Show]]] = {}
    for show in shows:
        grouped.setdefault(show.date_code, {}).setdefault(show.venue, []).append(show)
    return grouped


def _price(show: Show) -> str:
    if show.min_price is None:
        return ""
    if show.max_price and show.max_price > show.min_price:
        return f" — ₹{show.min_price:.0f}–{show.max_price:.0f}"
    return f" — ₹{show.min_price:.0f}"


def build_alert(watch: Watch, result: CheckResult) -> tuple[str, str, str, str]:
    """Return (title, telegram_html, plain_text, book_url)."""
    snap = result.snapshot
    title_text = (snap.title if snap and snap.title else watch.movie)
    shows = result.fresh or result.matched

    lines_html = [f"🚨 <b>TICKETS OPEN</b> — {esc(title_text)}"]
    lines_plain = [f"TICKETS OPEN — {title_text}"]

    shown = 0
    for date_code, venues in sorted(_group(shows).items()):
        lines_html.append(f"\n📅 <b>{esc(_pretty_date(date_code))}</b>")
        lines_plain.append(_pretty_date(date_code))
        for venue, venue_shows in venues.items():
            lines_html.append(f"📍 <b>{esc(venue)}</b>")
            lines_plain.append(venue)
            for show in venue_shows:
                if shown >= MAX_SHOWS_IN_MESSAGE:
                    continue
                seats = f" · {show.seats_avail} seats" if show.seats_avail else ""
                if show.sold_out:
                    seats = " · sold out"
                lines_html.append(f"   • {esc(show.label)}{esc(_price(show))}{esc(seats)}")
                lines_plain.append(f"  {show.label}{_price(show)}{seats}")
                shown += 1

    if len(shows) > shown:
        lines_html.append(f"\n<i>…and {len(shows) - shown} more shows</i>")

    date_for_url = shows[0].date_code if shows else datetime.now().strftime("%Y%m%d")
    book_url = (snap.book_url(watch.city, watch.region_code, date_for_url)
                if snap else "https://in.bookmyshow.com")
    lines_html.append(f'\n<a href="{book_url}">Open on BookMyShow →</a>')
    lines_plain.append(book_url)

    return (f"🎟 {title_text}", "\n".join(lines_html), "\n".join(lines_plain), book_url)


def handle_result(watch: Watch, result: CheckResult, state: State,
                  notifier: Notifier, verbose: bool = True) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    icon = {"open": "🎟", "closed": "⏳", "not_listed": "🔍",
            "no_match": "🙈", "error": "⚠️"}.get(result.status, "•")
    if verbose:
        print(f"{stamp} {icon} {result.summary}"
              + (f" [{result.error}]" if result.error else ""), flush=True)

    state.record_check(watch.id, result.status)

    if not result.should_alert:
        state.save()
        return

    title, body_html, plain, book_url = build_alert(watch, result)
    state.mark_alerted(watch.id, [s.fingerprint for s in result.fresh])
    state.save()
    notifier.alert(watch.id, title, body_html, plain, book_url)
