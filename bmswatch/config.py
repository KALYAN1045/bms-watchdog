"""Watchlist loading and validation."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

_ENV_RE = re.compile(r"\$\{([A-Z0-9_]+)\}")

# Show-time slots people actually think in. Late night is its own slot rather
# than wrapping past midnight, because 1 AM and 4 AM benefit shows are a real
# thing for big Telugu releases and you may want only those.
TIME_SLOTS: Dict[str, Dict[str, Any]] = {
    "morning":   {"from": 6 * 60,  "to": 11 * 60 + 59, "icon": "🌅",
                  "label": "Morning",    "when": "6 AM – 12 PM"},
    "afternoon": {"from": 12 * 60, "to": 16 * 60 + 59, "icon": "☀️",
                  "label": "Afternoon",  "when": "12 – 5 PM"},
    "evening":   {"from": 17 * 60, "to": 20 * 60 + 59, "icon": "🌆",
                  "label": "Evening",    "when": "5 – 9 PM"},
    "night":     {"from": 21 * 60, "to": 23 * 60 + 59, "icon": "🌙",
                  "label": "Night",      "when": "9 PM – 12 AM"},
    "latenight": {"from": 0,       "to": 5 * 60 + 59,  "icon": "🌃",
                  "label": "Late night", "when": "12 – 6 AM"},
}
SLOT_ORDER = ["morning", "afternoon", "evening", "night", "latenight"]


def slot_label(name: str) -> str:
    slot = TIME_SLOTS.get(name)
    return f"{slot['icon']} {slot['label']}" if slot else name


def describe_slots(names: List[str]) -> str:
    if not names:
        return "Any time"
    return ", ".join(slot_label(n) for n in names if n in TIME_SLOTS) or "Any time"


class ConfigError(ValueError):
    pass


def _expand(value: Any) -> Any:
    """Replace ${VAR} with the environment value, recursively."""
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


def _date_code(value: Any) -> str:
    """Accept 2026-09-25, 25-09-2026 or 20260925 -> '20260925'."""
    if isinstance(value, date):
        return value.strftime("%Y%m%d")
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y%m%d")
        except ValueError:
            continue
    raise ConfigError(f"unrecognised date {value!r} (use YYYY-MM-DD)")


def _minutes(value: str) -> int:
    """'08:00 AM' or '20:30' -> minutes since midnight."""
    text = str(value).strip().upper()
    for fmt in ("%I:%M %p", "%H:%M", "%I:%M%p"):
        try:
            parsed = datetime.strptime(text, fmt)
            return parsed.hour * 60 + parsed.minute
        except ValueError:
            continue
    raise ConfigError(f"unrecognised time {value!r} (use HH:MM)")


date_code = _date_code   # public alias


@dataclass
class Watch:
    id: str
    movie: str
    event_code: Optional[str] = None
    search: Optional[str] = None
    city: str = "hyderabad"
    region_code: str = "HYD"
    dates: List[str] = field(default_factory=list)   # empty => any open date
    max_dates: int = 5
    theatres: List[str] = field(default_factory=list)
    exclude_theatres: List[str] = field(default_factory=list)
    formats: List[str] = field(default_factory=list)
    languages: List[str] = field(default_factory=list)
    require_seats: bool = False
    min_seats: int = 1
    slots: List[str] = field(default_factory=list)          # named time slots
    windows: List[Tuple[int, int]] = field(default_factory=list)  # minutes, inclusive
    enabled: bool = True
    chat_id: str = ""            # set for watches created from Telegram

    def resolved_dates(self, open_dates: List[str]) -> List[str]:
        if self.dates:
            return self.dates
        return [d for d in open_dates][: self.max_dates]


def _proxy(value: Any) -> Optional[Dict[str, str]]:
    """'http://user:pass@host:port' -> the dict Playwright wants."""
    if not value:
        return None
    if isinstance(value, dict):
        return {k: str(v) for k, v in value.items() if v}
    from urllib.parse import urlparse

    parsed = urlparse(str(value))
    if not parsed.hostname:
        raise ConfigError(f"proxy {value!r} must look like http://host:port")
    server = f"{parsed.scheme or 'http'}://{parsed.hostname}"
    if parsed.port:
        server += f":{parsed.port}"
    proxy = {"server": server}
    if parsed.username:
        proxy["username"] = parsed.username
    if parsed.password:
        proxy["password"] = parsed.password
    return proxy


@dataclass
class TelegramConfig:
    """Alerts arrive in bursts: a few pings in quick succession to actually
    get your attention, then the whole burst repeats after a longer gap."""
    bot_token: str = ""
    chat_id: str = ""
    burst_size: int = 3               # pings in one burst
    burst_gap_seconds: int = 20       # spacing between those pings
    repeat_count: int = 3             # how many bursts before giving up
    repeat_every_seconds: int = 1800  # gap between bursts

    @property
    def total_pings(self) -> int:
        return max(1, self.burst_size) * max(1, self.repeat_count)

    @property
    def configured(self) -> bool:
        return bool(self.bot_token and self.chat_id)


@dataclass
class DesktopConfig:
    enabled: bool = True
    sound: str = "Sosumi"
    repeat_count: int = 8
    repeat_every_seconds: int = 30


@dataclass
class Settings:
    poll_seconds: int = 60
    jitter_seconds: int = 10
    heartbeat_minutes: int = 0        # 0 = off
    state_file: str = "state.json"
    default_city: str = "hyderabad"   # used before any watch exists
    default_region: str = "HYD"
    engine: str = "auto"              # auto | http | browser
    profile_dir: str = ".chrome-profile"
    channel: str = "chrome"
    window: str = "minimized"
    proxy: Optional[Dict[str, str]] = None
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    desktop: DesktopConfig = field(default_factory=DesktopConfig)
    watches: List[Watch] = field(default_factory=list)
    store_path: str = "watches.json"


def _build_watch(raw: Dict[str, Any], defaults: Dict[str, Any], index: int) -> Watch:
    merged = {**defaults, **raw}
    movie = merged.get("movie") or merged.get("title")
    if not movie:
        raise ConfigError(f"watch #{index + 1} is missing 'movie'")
    if not merged.get("event_code") and not merged.get("search"):
        merged["search"] = movie

    raw_dates = merged.get("dates") or []
    if isinstance(raw_dates, str):
        raw_dates = [] if raw_dates.strip().lower() in ("any", "all", "") else [raw_dates]
    dates = [_date_code(d) for d in raw_dates]

    next_days = merged.get("next_days")
    if not dates and next_days:
        today = date.today()
        dates = [(today + timedelta(days=i)).strftime("%Y%m%d") for i in range(int(next_days))]

    # named slots (from the bot) and/or an explicit range (hand-written YAML)
    raw_slots = merged.get("slots") or merged.get("time_slots") or []
    if isinstance(raw_slots, str):
        raw_slots = [raw_slots]
    slots: List[str] = []
    windows: List[Tuple[int, int]] = []
    for name in raw_slots:
        key = str(name).strip().lower().replace(" ", "").replace("-", "")
        if key not in TIME_SLOTS:
            raise ConfigError(
                f"watch '{movie}': unknown time slot {name!r}. "
                f"Use any of: {', '.join(SLOT_ORDER)}")
        if key not in slots:
            slots.append(key)
            windows.append((TIME_SLOTS[key]["from"], TIME_SLOTS[key]["to"]))

    window = merged.get("time_between") or merged.get("time_window")
    if window:
        if not isinstance(window, (list, tuple)) or len(window) != 2:
            raise ConfigError(f"watch '{movie}': time_between must be [start, end]")
        windows.append((_minutes(window[0]), _minutes(window[1])))

    def as_list(key: str) -> List[str]:
        value = merged.get(key) or []
        if isinstance(value, str):
            value = [value]
        return [str(v).strip() for v in value if str(v).strip()]

    return Watch(
        id=str(merged.get("id") or re.sub(r"[^a-z0-9]+", "-", movie.lower()).strip("-")),
        movie=movie,
        event_code=(merged.get("event_code") or "").strip() or None,
        search=merged.get("search"),
        city=str(merged.get("city", "hyderabad")).strip().lower(),
        region_code=str(merged.get("region_code", "HYD")).strip().upper(),
        dates=dates,
        max_dates=int(merged.get("max_dates", 5)),
        theatres=as_list("theatres"),
        exclude_theatres=as_list("exclude_theatres"),
        formats=as_list("formats"),
        languages=as_list("languages"),
        require_seats=bool(merged.get("require_seats", False)),
        min_seats=int(merged.get("min_seats", 1)),
        slots=slots,
        windows=windows,
        enabled=bool(merged.get("enabled", True)),
        chat_id=str(merged.get("chat_id") or ""),
    )


def _store_watches(store_path: Path) -> List[Dict[str, Any]]:
    if not store_path.exists():
        return []
    try:
        blob = json.loads(store_path.read_text())
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{store_path.name} is not readable JSON: {exc}") from None
    return [w for w in (blob.get("watches") or []) if isinstance(w, dict)]


def load(path: str | Path) -> Settings:
    path = Path(path)
    if not path.exists():
        raise ConfigError(f"watchlist not found: {path}")
    data = _expand(yaml.safe_load(path.read_text()) or {})

    defaults = data.get("defaults") or {}
    raw_watches = data.get("watches") or []

    tg_raw = data.get("telegram") or {}
    desktop_raw = data.get("desktop") or {}

    settings = Settings(
        poll_seconds=int(defaults.get("poll_seconds", 60)),
        jitter_seconds=int(defaults.get("jitter_seconds", 10)),
        heartbeat_minutes=int(defaults.get("heartbeat_minutes", 0)),
        state_file=str(defaults.get("state_file", "state.json")),
        default_city=str(defaults.get("city", "hyderabad")).strip().lower(),
        default_region=str(defaults.get("region_code", "HYD")).strip().upper(),
        engine=os.environ.get("BMS_ENGINE") or str(defaults.get("engine", "auto")),
        profile_dir=str(defaults.get("profile_dir", ".chrome-profile")),
        # env overrides let one watchlist.yaml serve both a laptop and a
        # container without editing it (the Docker image sets these)
        channel=os.environ.get("BMS_CHANNEL") or str(defaults.get("channel", "chrome")),
        window=os.environ.get("BMS_WINDOW") or str(defaults.get("window", "minimized")),
        proxy=_proxy(os.environ.get("BMS_PROXY") or defaults.get("proxy")),
        telegram=TelegramConfig(
            bot_token=str(tg_raw.get("bot_token", "")).strip(),
            chat_id=str(tg_raw.get("chat_id", "")).strip(),
            burst_size=max(1, int(tg_raw.get("burst_size", 3))),
            burst_gap_seconds=max(5, int(tg_raw.get("burst_gap_seconds", 20))),
            repeat_count=max(1, int(tg_raw.get("repeat_count", 3))),
            repeat_every_seconds=max(30, int(tg_raw.get("repeat_every_seconds", 1800))),
        ),
        desktop=DesktopConfig(
            enabled=(os.environ.get("BMS_DESKTOP", "1") != "0"
                     and bool(desktop_raw.get("enabled", True))),
            sound=str(desktop_raw.get("sound", "Sosumi")),
            repeat_count=int(desktop_raw.get("repeat_count", 8)),
            repeat_every_seconds=int(desktop_raw.get("repeat_every_seconds", 30)),
        ),
        store_path=str(defaults.get("store_path", "watches.json")),
        watches=[_build_watch(w, defaults, i) for i, w in enumerate(raw_watches)],
    )

    # watches created from Telegram live in their own JSON file
    store_file = path.parent / settings.store_path
    for i, raw in enumerate(_store_watches(store_file)):
        settings.watches.append(_build_watch(raw, defaults, len(raw_watches) + i))

    seen = set()
    for watch in settings.watches:
        if watch.id in seen:
            raise ConfigError(f"duplicate watch id '{watch.id}' -- give each watch a unique 'id'")
        seen.add(watch.id)
    if settings.poll_seconds < 20:
        raise ConfigError("poll_seconds below 20 hammers BookMyShow; use 20 or more")
    return settings
