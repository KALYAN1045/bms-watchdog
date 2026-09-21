"""Tiny JSON store so a restart doesn't re-alert on shows you already saw."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, List, Set


class State:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._data: Dict[str, dict] = {}
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text()) or {}
            except (OSError, ValueError):
                self._data = {}

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(json.dumps(self._data, indent=1, sort_keys=True))
            tmp.replace(self.path)
        except OSError as exc:
            print(f"[state] could not save {self.path}: {exc}", flush=True)

    def _watch(self, watch_id: str) -> dict:
        if watch_id.startswith("__"):
            raise KeyError(f"{watch_id!r} is reserved")
        return self._data.setdefault(watch_id, {"alerted": [], "event_code": None,
                                                "last_check": 0, "last_result": ""})

    # -- alert de-duplication ---------------------------------------------

    def already_alerted(self, watch_id: str, fingerprints: List[str]) -> Set[str]:
        seen = set(self._watch(watch_id).get("alerted", []))
        return {f for f in fingerprints if f in seen}

    def new_fingerprints(self, watch_id: str, fingerprints: List[str]) -> List[str]:
        seen = set(self._watch(watch_id).get("alerted", []))
        return [f for f in fingerprints if f not in seen]

    def mark_alerted(self, watch_id: str, fingerprints: List[str]) -> None:
        entry = self._watch(watch_id)
        merged = set(entry.get("alerted", [])) | set(fingerprints)
        # keep it bounded; old shows fall out of BMS anyway
        entry["alerted"] = sorted(merged)[-4000:]

    # -- misc --------------------------------------------------------------

    def event_code(self, watch_id: str):
        return self._watch(watch_id).get("event_code")

    def set_event_code(self, watch_id: str, code: str) -> None:
        self._watch(watch_id)["event_code"] = code

    def record_check(self, watch_id: str, result: str) -> None:
        entry = self._watch(watch_id)
        entry["last_check"] = int(time.time())
        entry["last_result"] = result

    # -- alerts still working through their repeat schedule ---------------

    def pending(self) -> List[dict]:
        return list(self._data.get("__pending__", []))

    def set_pending(self, items: List[dict]) -> None:
        if items:
            self._data["__pending__"] = items
        else:
            self._data.pop("__pending__", None)

    def get(self, key: str, default=None):
        return self._data.get("__meta__", {}).get(key, default)

    def set(self, key: str, value) -> None:
        self._data.setdefault("__meta__", {})[key] = value
