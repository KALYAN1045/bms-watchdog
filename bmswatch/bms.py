"""Turn BookMyShow's showtimes payload into something we can match against."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

BOOK_URL = "https://in.bookmyshow.com/buytickets/{slug}-{city}/movie-{region}-{event}-MT/{date}"

# Availability codes seen on ShowTimes.
_SOLD_OUT = {"S", "N"}


@dataclass
class SeatCategory:
    """One price block within a show — "GOLD", "PLATINUM", "DIRECTOR CHOICE".

    A blocked-off centre section shows up here as a category with
    seats_avail 0 out of a non-zero total.
    """
    desc: str
    price: Optional[float]
    seats_avail: int
    max_seats: int

    @property
    def sold_out(self) -> bool:
        return self.seats_avail <= 0

    @property
    def label(self) -> str:
        price = f" ₹{self.price:.0f}" if self.price is not None else ""
        return f"{self.desc}{price}"


@dataclass
class Show:
    date_code: str          # "20260925"
    venue: str              # "Prasads Multiplex: Hyderabad"
    venue_code: str         # "PRHY"
    time: str               # "08:00 AM"
    event_code: str         # child event code -- identifies format+language
    fmt: str                # "2D" / "IMAX 2D" / "EPIQ" / "4DX"
    language: str           # "Telugu"
    attributes: str         # "PCX SCREEN", "LASER DOLBY ATMOS", ...
    availability: str       # "A" available, "S" sold out, ...
    seats_avail: int
    min_price: Optional[float]
    max_price: Optional[float]
    categories: List[SeatCategory] = field(default_factory=list)

    def category(self, needle: str) -> Optional[SeatCategory]:
        low = needle.lower()
        for cat in self.categories:
            if low in cat.desc.lower():
                return cat
        return None

    @property
    def sold_out(self) -> bool:
        return self.availability in _SOLD_OUT or self.seats_avail <= 0

    @property
    def fingerprint(self) -> str:
        """Stable id for de-duplicating alerts about the same show."""
        return f"{self.date_code}|{self.venue_code}|{self.time}|{self.event_code}"

    @property
    def label(self) -> str:
        bits = [self.time, self.fmt]
        if self.attributes:
            bits.append(self.attributes)
        if self.language:
            bits.append(self.language)
        return " · ".join(b for b in bits if b)


@dataclass
class Snapshot:
    event_code: str
    title: str
    slug: str
    requested_date: str
    returned_dates: List[str] = field(default_factory=list)
    open_dates: List[str] = field(default_factory=list)   # everything BMS will sell
    shows: List[Show] = field(default_factory=list)

    @property
    def booking_open(self) -> bool:
        return bool(self.shows)

    def book_url(self, city: str, region: str, date_code: str) -> str:
        return BOOK_URL.format(
            slug=self.slug or "movie",
            city=city,
            region=region.lower(),
            event=self.event_code,
            date=date_code,
        )


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _categories(show: Dict[str, Any]) -> List[SeatCategory]:
    out = []
    for cat in show.get("Categories") or []:
        desc = (cat.get("PriceDesc") or "").strip()
        if not desc:
            continue
        out.append(SeatCategory(desc=desc, price=_as_float(cat.get("CurPrice")),
                                seats_avail=_int(cat.get("SeatsAvail")),
                                max_seats=_int(cat.get("MaxSeats"))))
    return out


def _seats(show: Dict[str, Any]) -> int:
    return sum(_int(c.get("SeatsAvail")) for c in (show.get("Categories") or []))


def parse_showtimes(payload: Dict[str, Any], event_code: str, requested_date: str) -> Snapshot:
    """Build a Snapshot, keeping only shows that really are on `requested_date`.

    BookMyShow happily answers a date it has no shows for by returning the next
    date that does have them -- so the date must be re-checked, not assumed.
    """
    details = payload.get("ShowDetails") or []
    open_dates = [
        d.get("DateCode")
        for d in (payload.get("ShowDatesArray") or [])
        if d.get("DateCode") and not d.get("isDisabled")
    ]

    snap = Snapshot(
        event_code=event_code,
        title="",
        slug="",
        requested_date=requested_date,
        returned_dates=[d.get("Date", "") for d in details],
        open_dates=open_dates,
    )

    for detail in details:
        event = detail.get("Event") or {}
        snap.title = snap.title or event.get("EventTitle") or ""

        # child events carry the format/language for each showtime
        child_by_code: Dict[str, Dict[str, str]] = {}
        for child in event.get("ChildEvents") or []:
            code = child.get("EventCode")
            if code:
                child_by_code[code] = {
                    "fmt": child.get("EventDimension") or "",
                    "lang": child.get("EventLang") or "",
                    "slug": child.get("EventUrl") or "",
                }
                snap.slug = snap.slug or child.get("EventUrl") or ""

        date_code = detail.get("Date") or ""
        if date_code != requested_date:
            continue  # a substitute date, not what we asked for

        for venue in detail.get("Venues") or []:
            if str(venue.get("AllowSales", "Y")).upper() == "N":
                continue
            name = venue.get("VenueName") or ""
            code = venue.get("VenueCode") or ""
            for st in venue.get("ShowTimes") or []:
                child = child_by_code.get(st.get("EventCode", ""), {})
                snap.shows.append(
                    Show(
                        date_code=st.get("ShowDateCode") or date_code,
                        venue=name,
                        venue_code=code,
                        time=st.get("ShowTime") or "",
                        event_code=st.get("EventCode") or event_code,
                        fmt=child.get("fmt", ""),
                        language=child.get("lang", ""),
                        attributes=(st.get("Attributes") or "").strip(),
                        availability=(st.get("Availability") or "").strip().upper(),
                        seats_avail=_seats(st),
                        min_price=_as_float(st.get("MinPrice")),
                        max_price=_as_float(st.get("MaxPrice")),
                        categories=_categories(st),
                    )
                )

    return snap
