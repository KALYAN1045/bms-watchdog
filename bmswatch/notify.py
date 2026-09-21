"""Notification channels: Telegram (with acknowledge-to-stop) and macOS desktop."""

from __future__ import annotations

import html
import platform
import shutil
import subprocess
import time
from typing import List, Optional

import requests

from .config import DesktopConfig, TelegramConfig

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


def redact(text: str, token: str) -> str:
    """Telegram puts the token in the URL, so it turns up in network errors."""
    return str(text).replace(token, "<token>") if token else str(text)
_ACK_WORDS = {"ack", "ok", "okay", "got it", "stop", "/ack", "/stop"}


class Telegram:
    def __init__(self, cfg: TelegramConfig, timeout: int = 15):
        self.cfg = cfg
        self.timeout = timeout
        self._offset: Optional[int] = None

    # -- low level ---------------------------------------------------------

    def _call(self, method: str, **payload):
        url = TELEGRAM_API.format(token=self.cfg.bot_token, method=method)
        # getUpdates holds the connection open for its own `timeout` seconds,
        # so the HTTP read timeout has to outlast it or every poll dies early.
        http_timeout = self.timeout
        if isinstance(payload.get("timeout"), int):
            http_timeout = max(http_timeout, payload["timeout"] + 10)
        try:
            res = requests.post(url, json=payload, timeout=http_timeout)
            data = res.json()
        except Exception as exc:
            raise RuntimeError(
                f"Telegram {method} failed: "
                f"{redact(exc, self.cfg.bot_token)}") from None
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {data.get('description')}")
        return data.get("result")

    def send(self, text: str, buttons: Optional[List[dict]] = None, silent: bool = False):
        payload = {
            "chat_id": self.cfg.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "disable_notification": silent,
        }
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": [buttons]}
        return self._call("sendMessage", **payload)

    def drain(self) -> None:
        """Skip whatever is already queued so old messages can't fake an ack."""
        try:
            updates = self._call("getUpdates", offset=-1, timeout=0) or []
        except Exception:
            return
        if updates:
            self._offset = updates[-1]["update_id"] + 1

    def check_ack(self, watch_id: str) -> bool:
        """True if the user tapped the button or replied with an ack word."""
        try:
            kwargs = {"timeout": 0}
            if self._offset is not None:
                kwargs["offset"] = self._offset
            updates = self._call("getUpdates", **kwargs) or []
        except Exception:
            return False

        acked = False
        for update in updates:
            self._offset = update["update_id"] + 1
            callback = update.get("callback_query")
            if callback:
                data = callback.get("data", "")
                try:
                    self._call("answerCallbackQuery", callback_query_id=callback["id"],
                               text="Stopped pinging ✔")
                except Exception:
                    pass
                if data in (f"ack:{watch_id}", "ack:*"):
                    acked = True
                continue
            message = (update.get("message") or {}).get("text", "").strip().lower()
            if message and (message in _ACK_WORDS or watch_id.lower() in message):
                acked = True
        return acked


class Desktop:
    """macOS notification-centre banner plus a sound, via osascript/afplay."""

    def __init__(self, cfg: DesktopConfig):
        self.cfg = cfg
        self.available = (
            cfg.enabled
            and platform.system() == "Darwin"
            and shutil.which("osascript") is not None
        )

    def send(self, title: str, body: str) -> None:
        if not self.available:
            return
        script = 'display notification {body} with title {title} sound name {sound}'.format(
            body=_as_applescript(body[:220]),
            title=_as_applescript(title[:100]),
            sound=_as_applescript(self.cfg.sound),
        )
        try:
            subprocess.run(["osascript", "-e", script], check=False, timeout=10,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


def _as_applescript(text: str) -> str:
    return '"%s"' % text.replace("\\", "\\\\").replace('"', '\\"')


class Notifier:
    def __init__(self, telegram_cfg: TelegramConfig, desktop_cfg: DesktopConfig,
                 verbose: bool = False):
        self.telegram = Telegram(telegram_cfg) if telegram_cfg.configured else None
        self.desktop = Desktop(desktop_cfg)
        self.repeat_count = max(1, telegram_cfg.repeat_count)
        self.repeat_every = max(5, telegram_cfg.repeat_every_seconds)
        self.verbose = verbose

    @property
    def channels(self) -> List[str]:
        names = []
        if self.telegram:
            names.append("telegram")
        if self.desktop.available:
            names.append("desktop")
        return names

    def info(self, text: str, silent: bool = True) -> None:
        if self.telegram:
            try:
                self.telegram.send(text, silent=silent)
            except Exception as exc:
                print(f"[notify] telegram info failed: {exc}", flush=True)

    def alert(self, watch_id: str, title: str, body_html: str, plain: str,
              book_url: str) -> None:
        """Ping repeatedly until the user acknowledges or we run out of rounds."""
        if not self.channels:
            print(f"[notify] no channel configured -- would have alerted:\n{plain}", flush=True)
            return

        buttons = [
            {"text": "🎟 Book now", "url": book_url},
            {"text": "✅ Got it", "callback_data": f"ack:{watch_id}"},
        ]
        if self.telegram:
            self.telegram.drain()

        for round_no in range(1, self.repeat_count + 1):
            prefix = "" if round_no == 1 else f"🔁 <b>Reminder {round_no}/{self.repeat_count}</b>\n\n"
            if self.telegram:
                try:
                    self.telegram.send(prefix + body_html, buttons=buttons)
                except Exception as exc:
                    print(f"[notify] telegram send failed: {exc}", flush=True)
            self.desktop.send(title, plain)

            if round_no == self.repeat_count:
                break

            # wait out the interval, checking for an ack every couple of seconds
            waited = 0
            while waited < self.repeat_every:
                time.sleep(2)
                waited += 2
                if self.telegram and self.telegram.check_ack(watch_id):
                    if self.verbose:
                        print(f"[notify] '{watch_id}' acknowledged", flush=True)
                    self.info("🔕 Okay, I'll stop pinging about this one.", silent=True)
                    return


def esc(text: str) -> str:
    return html.escape(str(text), quote=False)
