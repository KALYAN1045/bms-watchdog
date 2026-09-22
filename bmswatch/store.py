"""Watches created from Telegram, kept in JSON so the bot can edit them.

The YAML watchlist is still read and still works; these are merged on top of
it. Anything the bot creates lives here, so a hand-written watchlist.yaml is
never rewritten by a program.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:40] or "watch"


class WatchStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._data: Dict[str, Any] = {"users": {}, "watches": []}
        self.load()

    # -- persistence -------------------------------------------------------

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            blob = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        if isinstance(blob, dict):
            self._data["users"] = blob.get("users") or {}
            self._data["watches"] = [w for w in (blob.get("watches") or [])
                                     if isinstance(w, dict)]

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._data, indent=1, sort_keys=True))
            tmp.replace(self.path)
        except OSError as exc:
            print(f"[store] could not save {self.path}: {exc}", flush=True)

    # -- per-user preferences ---------------------------------------------

    def get_user(self, chat_id: Any) -> Dict[str, Any]:
        return self._data["users"].get(str(chat_id), {})

    def set_city(self, chat_id: Any, *, name: str, code: str, slug: str) -> None:
        user = self._data["users"].setdefault(str(chat_id), {})
        user.update({"city_name": name, "region_code": code, "city": slug})
        self.save()

    # -- watches -----------------------------------------------------------

    def watches(self, chat_id: Any = None) -> List[Dict[str, Any]]:
        if chat_id is None:
            return list(self._data["watches"])
        return [w for w in self._data["watches"] if str(w.get("chat_id")) == str(chat_id)]

    def get(self, watch_id: str) -> Optional[Dict[str, Any]]:
        for w in self._data["watches"]:
            if w.get("id") == watch_id:
                return w
        return None

    def _unique_id(self, base: str) -> str:
        taken = {w.get("id") for w in self._data["watches"]}
        if base not in taken:
            return base
        for n in range(2, 100):
            candidate = f"{base}-{n}"
            if candidate not in taken:
                return candidate
        return f"{base}-{int(time.time())}"

    def add(self, watch: Dict[str, Any]) -> str:
        watch = dict(watch)
        watch["id"] = self._unique_id(_slug(watch.get("id") or watch.get("movie", "watch")))
        watch.setdefault("created", int(time.time()))
        watch.setdefault("enabled", True)
        self._data["watches"].append(watch)
        self.save()
        return watch["id"]

    def remove(self, watch_id: str) -> bool:
        before = len(self._data["watches"])
        self._data["watches"] = [w for w in self._data["watches"]
                                 if w.get("id") != watch_id]
        changed = len(self._data["watches"]) != before
        if changed:
            self.save()
        return changed

    def set_enabled(self, watch_id: str, enabled: bool) -> bool:
        watch = self.get(watch_id)
        if not watch:
            return False
        watch["enabled"] = enabled
        self.save()
        return True

    def duplicate_of(self, chat_id: Any, event_code: str, theatres: List[str],
                     dates: List[str], slots: Optional[List[str]] = None,
                     categories: Optional[List[str]] = None) -> Optional[str]:
        """Existing watch with the same movie, theatres, dates and timings."""
        slots = slots or []
        for w in self.watches(chat_id):
            if (w.get("event_code") == event_code
                    and sorted(w.get("theatres") or []) == sorted(theatres)
                    and sorted(w.get("dates") or []) == sorted(dates)
                    and sorted(w.get("slots") or []) == sorted(slots)
                    and sorted(w.get("categories") or []) == sorted(categories or [])):
                return w.get("id")
        return None
