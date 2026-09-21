"""Cities and movie listings — what the bot offers people to choose from."""

from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

REGIONS_API = "https://in.bookmyshow.com/api/explore/v1/discover/regions"
REGION_CACHE_HOURS = 24 * 7          # the list of Indian cities is not volatile


@dataclass
class Region:
    name: str          # "Hyderabad"
    code: str          # "HYD"   (some are words, e.g. "MUMBAI")
    slug: str          # "hyderabad"
    lat: float
    lon: float
    state: str = ""
    aliases: List[str] = field(default_factory=list)
    top: bool = False

    @property
    def label(self) -> str:
        return f"{self.name}, {self.state}" if self.state else self.name

    def to_json(self) -> dict:
        return {"name": self.name, "code": self.code, "slug": self.slug,
                "lat": self.lat, "lon": self.lon, "state": self.state,
                "aliases": self.aliases, "top": self.top}

    @classmethod
    def from_json(cls, d: dict) -> "Region":
        return cls(**d)


@dataclass
class Movie:
    code: str          # "ET00436621"
    slug: str          # "the-paradise"
    title: str         # "The Paradise"
    upcoming: bool = False

    @property
    def label(self) -> str:
        return self.title or self.slug.replace("-", " ").title()


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_regions(payload: Dict[str, Any]) -> List[Region]:
    root = payload.get("BookMyShow") or {}
    regions: List[Region] = []
    for key, is_top in (("TopCities", True), ("OtherCities", False)):
        for raw in root.get(key) or []:
            if str(raw.get("AllowSales", "Y")).upper() != "Y":
                continue
            name = (raw.get("RegionName") or "").strip()
            code = (raw.get("RegionCode") or "").strip()
            if not name or not code:
                continue
            regions.append(Region(
                name=name,
                code=code,
                slug=(raw.get("RegionSlug") or name.lower().replace(" ", "-")).strip(),
                lat=_float(raw.get("Lat")),
                lon=_float(raw.get("Long")),
                state=(raw.get("StateName") or "").strip(),
                aliases=[a.lower() for a in (raw.get("Alias") or []) if a],
                top=is_top,
            ))
    return regions


def load_regions(session, cache_path: Optional[Path] = None,
                 force: bool = False) -> List[Region]:
    """All bookable cities, cached on disk because the list barely changes."""
    cache_path = Path(cache_path) if cache_path else None
    if cache_path and cache_path.exists() and not force:
        try:
            blob = json.loads(cache_path.read_text())
            fresh = time.time() - blob.get("fetched", 0) < REGION_CACHE_HOURS * 3600
            if fresh and blob.get("regions"):
                return [Region.from_json(r) for r in blob["regions"]]
        except (OSError, ValueError, TypeError):
            pass

    status, body = session._get(REGIONS_API, {"accept": "application/json"}) \
        if hasattr(session, "_get") else (200, session.page_html(REGIONS_API))
    if status != 200:
        raise RuntimeError(f"regions API returned HTTP {status}")
    regions = _parse_regions(json.loads(body))

    if cache_path and regions:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(
                {"fetched": time.time(), "regions": [r.to_json() for r in regions]}))
        except OSError:
            pass
    return regions


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def nearest_regions(regions: List[Region], lat: float, lon: float,
                    limit: int = 3) -> List[tuple]:
    """[(Region, distance_km), …] closest first."""
    scored = [(r, haversine_km(lat, lon, r.lat, r.lon)) for r in regions if r.lat or r.lon]
    scored.sort(key=lambda pair: pair[1])
    return scored[:limit]


def search_regions(regions: List[Region], query: str, limit: int = 8) -> List[Region]:
    """Match on name, slug or BookMyShow's own alias list."""
    q = query.strip().lower()
    if not q:
        return []
    exact, starts, contains = [], [], []
    for r in regions:
        haystack = [r.name.lower(), r.slug] + r.aliases
        if any(h == q for h in haystack):
            exact.append(r)
        elif any(h.startswith(q) for h in haystack):
            starts.append(r)
        elif any(q in h for h in haystack):
            contains.append(r)
    ordered = exact + starts + contains
    ordered.sort(key=lambda r: (not r.top, r.name))
    return ordered[:limit]


def top_regions(regions: List[Region]) -> List[Region]:
    return [r for r in regions if r.top]


# -- movie listings --------------------------------------------------------

_ANCHOR = r'href="https://in\.bookmyshow\.com/movies/%s/([a-z0-9\-]+)/(ET\d+)"'
_TITLE_NEAR = re.compile(r'(?:data-content|alt)="([^"]{1,120})"')


def parse_movie_listing(html: str, city_slug: str, upcoming: bool = False) -> List[Movie]:
    """Pull {code, slug, title} out of a listing page.

    The title sits in a data-content/alt attribute just after each link, so we
    look a short way past the anchor rather than trying to parse the markup.
    """
    movies: Dict[str, Movie] = {}
    pattern = re.compile(_ANCHOR % re.escape(city_slug))
    for match in pattern.finditer(html):
        slug, code = match.group(1), match.group(2)
        if code in movies:
            continue
        window = html[match.end(): match.end() + 900]
        found = _TITLE_NEAR.search(window)
        title = found.group(1).strip() if found else ""
        if not title or title.lower() in ("", "poster", "image"):
            title = slug.replace("-", " ").title()
        movies[code] = Movie(code=code, slug=slug, title=title, upcoming=upcoming)
    return list(movies.values())


def load_movies(session, city_slug: str) -> List[Movie]:
    """Now-showing first, then anything upcoming that isn't already listed."""
    from .fetcher import EXPLORE_NOW, EXPLORE_UPCOMING, FetchError

    movies: Dict[str, Movie] = {}
    for url, upcoming in ((EXPLORE_NOW.format(city=city_slug), False),
                          (EXPLORE_UPCOMING.format(city=city_slug), True)):
        try:
            html = session.page_html(url)
        except FetchError:
            continue
        for movie in parse_movie_listing(html, city_slug, upcoming=upcoming):
            movies.setdefault(movie.code, movie)
    return sorted(movies.values(), key=lambda m: (m.upcoming, m.label.lower()))
