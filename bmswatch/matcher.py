"""Decide which showtimes a watch actually cares about."""

from __future__ import annotations

import re
from datetime import datetime
from functools import lru_cache
from typing import List

from .bms import Show
from .config import Watch


def _minutes(show_time: str) -> int:
    for fmt in ("%I:%M %p", "%H:%M"):
        try:
            parsed = datetime.strptime(show_time.strip().upper(), fmt)
            return parsed.hour * 60 + parsed.minute
        except ValueError:
            continue
    return -1


@lru_cache(maxsize=512)
def _needle_re(needle: str) -> "re.Pattern":
    """Whole-word match, so 'AMB' finds "AMB Cinemas" but not "Bhramaramba".

    Punctuation and spacing in the needle are treated loosely, so both
    "AAA Cinemas" and "aaa-cinemas" match "AAA Cinemas: Ameerpet".
    """
    parts = [re.escape(p) for p in re.split(r"[^A-Za-z0-9]+", needle) if p]
    if not parts:
        return re.compile(r"(?!x)x")          # matches nothing
    body = r"[^A-Za-z0-9]+".join(parts)
    return re.compile(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % body, re.IGNORECASE)


def _contains_any(haystack: str, needles: List[str]) -> bool:
    return any(_needle_re(n).search(haystack) for n in needles)


def matches(watch: Watch, show: Show) -> bool:
    if watch.theatres and not _contains_any(show.venue, watch.theatres):
        return False
    if watch.exclude_theatres and _contains_any(show.venue, watch.exclude_theatres):
        return False

    if watch.formats:
        # A format can show up either as the child event's dimension ("IMAX 2D",
        # "EPIQ") or as a screen attribute ("PCX SCREEN", "LASER DOLBY ATMOS").
        haystack = f"{show.fmt} {show.attributes}"
        if not _contains_any(haystack, watch.formats):
            return False

    if watch.languages and not _contains_any(show.language, watch.languages):
        return False

    if watch.require_seats and show.seats_avail < watch.min_seats:
        return False

    if watch.time_from > 0 or watch.time_to < 24 * 60:
        mins = _minutes(show.time)
        if mins < 0 or not (watch.time_from <= mins <= watch.time_to):
            return False

    return True


def filter_shows(watch: Watch, shows: List[Show]) -> List[Show]:
    return [s for s in shows if matches(watch, s)]
