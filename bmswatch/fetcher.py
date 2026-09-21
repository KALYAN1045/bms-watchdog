"""Browser-backed BookMyShow client.

BookMyShow sits behind Cloudflare. Plain HTTP (curl, requests) gets a 403,
and so does every headless browser I could throw at it -- bundled Chromium,
Chrome's headless shell, Firefox headless *and* headful. The one combination
that is served normally is **real, installed Google Chrome running headful**
with the automation flags stripped off.

So that is what we drive, with the window minimised so it stays out of the
way, and every API call issued from inside the page so it carries the same
cookies and TLS fingerprint as the browsing that earned them.

A poll is then one small JSON request on an already-warm session.
"""

from __future__ import annotations

import json
import os
import random
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlencode

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

SHOWTIMES_API = "https://in.bookmyshow.com/api/movies-data/showtimes-by-event"
EXPLORE_NOW = "https://in.bookmyshow.com/explore/movies-{city}"
EXPLORE_UPCOMING = "https://in.bookmyshow.com/explore/upcoming-movies-{city}"
HOME = "https://in.bookmyshow.com/"

# The public token BookMyShow's own Android app sends. Not a secret, not tied
# to any account -- it only identifies the client to their read-only API.
APP_TOKEN = "67x1xa33b4x422b361ba"

CHROME_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--mute-audio",
    "--disable-background-timer-throttling",
    "--disable-renderer-backgrounding",
    "--disable-backgrounding-occluded-windows",
    "--no-first-run",
    "--no-default-browser-check",
]

_STEALTH = "Object.defineProperty(navigator,'webdriver',{get:()=>false});"

_JS_FETCH = """
async ([url, headers]) => {
  try {
    const r = await fetch(url, { headers, credentials: 'include' });
    return { status: r.status, body: await r.text() };
  } catch (e) {
    return { status: 0, body: String(e) };
  }
}
"""


class FetchError(RuntimeError):
    pass


class BMSSession:
    """A long-lived Chrome session against in.bookmyshow.com."""

    def __init__(
        self,
        city: str = "hyderabad",
        region_code: str = "HYD",
        lat: str = "17.3850",
        lon: str = "78.4867",
        profile_dir: Optional[Path] = None,
        channel: str = "chrome",
        window: str = "minimized",      # minimized | offscreen | visible
        proxy: Optional[dict] = None,
        verbose: bool = False,
        **_ignored: Any,
    ):
        self.city = city
        self.region_code = region_code
        self.lat = lat
        self.lon = lon
        self.profile_dir = Path(profile_dir) if profile_dir else Path(".chrome-profile")
        self.channel = channel
        self.window = window
        self.proxy = proxy
        self.verbose = verbose

        self._pw = None
        self._ctx = None
        self._page = None
        self._bms_id = "1.%d.%d" % (random.randint(10**8, 10**9), int(time.time() * 1000))

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> "BMSSession":
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"[fetcher] {msg}", flush=True)

    def start(self) -> None:
        self._pw = sync_playwright().start()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        # a previous crash can leave locks behind that stop Chrome reopening
        for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            try:
                (self.profile_dir / lock).unlink()
            except (OSError, FileNotFoundError):
                pass

        args = list(CHROME_ARGS)
        if os.environ.get("BMS_NO_SANDBOX") == "1":
            # containers: Chrome's setuid sandbox can't start, and /dev/shm is tiny
            args += ["--no-sandbox", "--disable-dev-shm-usage"]
        if self.window == "offscreen":
            args += ["--window-position=-3000,-3000", "--window-size=1000,700"]

        last_error: Optional[Exception] = None
        for channel in self._channel_candidates():
            try:
                kwargs: Dict[str, Any] = dict(
                    user_data_dir=str(self.profile_dir),
                    headless=False,
                    args=args,
                    ignore_default_args=["--enable-automation"],
                    locale="en-IN",
                    timezone_id="Asia/Kolkata",
                    viewport=None,
                )
                if channel:
                    kwargs["channel"] = channel
                if self.proxy:
                    kwargs["proxy"] = self.proxy
                self._ctx = self._pw.chromium.launch_persistent_context(**kwargs)
                self._log(f"launched {channel or 'bundled chromium'}")
                if not channel and self.channel.lower() not in (
                        "", "chromium", "bundled", "none"):
                    print("[fetcher] note: Google Chrome not found, using bundled "
                          "Chromium instead.", flush=True)
                break
            except Exception as exc:                        # channel not installed
                last_error = exc
                self._log(f"channel {channel!r} unavailable: {str(exc)[:100]}")
        if self._ctx is None:
            raise FetchError(f"could not launch a browser: {last_error}")

        self._ctx.add_init_script(_STEALTH)
        self._ctx.add_cookies(self._region_cookies())
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        self._apply_window_state()
        self._seed()

    def _channel_candidates(self) -> List[Optional[str]]:
        """Which browsers to try, best first. None = Playwright's own Chromium.

        Real Chrome is preferred (it is the least likely fingerprint to be
        challenged), but bundled Chromium works too as long as it runs headful
        -- which is what a Linux server needs, since Chrome ships amd64 only.
        """
        if self.channel.lower() in ("", "chromium", "bundled", "none"):
            return [None]
        order: List[Optional[str]] = [self.channel]
        for fallback in ("chrome", "chrome-beta", "msedge", None):
            if fallback not in order:
                order.append(fallback)
        return order

    def _apply_window_state(self) -> None:
        if self.window != "minimized":
            return
        try:
            cdp = self._ctx.new_cdp_session(self._page)
            window_id = cdp.send("Browser.getWindowForTarget")["windowId"]
            cdp.send("Browser.setWindowBounds",
                     {"windowId": window_id, "bounds": {"windowState": "minimized"}})
            self._log("window minimised")
        except Exception as exc:
            self._log(f"could not minimise window: {exc}")

    def close(self) -> None:
        try:
            if self._ctx:
                self._ctx.close()
        except Exception:
            pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._pw = self._ctx = self._page = None

    # -- session plumbing --------------------------------------------------

    def _region_cookies(self) -> List[dict]:
        """Pin the city, or BookMyShow serves the 'choose your city' page."""
        region = {
            "regionCode": self.region_code,
            "regionNameSlug": self.city,
            "regionCodeSlug": self.region_code.lower(),
            "regionName": self.city.replace("-", " ").title(),
            "subCode": "",
            "subName": "",
            "Lat": self.lat,
            "Long": self.lon,
            "countryCode": "IN",
        }
        common = {"domain": ".bookmyshow.com", "path": "/"}
        return [
            {"name": "rgn", "value": quote(json.dumps(region, separators=(",", ":"))), **common},
            {"name": "_region_code", "value": self.region_code, **common},
            {"name": "_region_slug", "value": self.city, **common},
        ]

    def _seed(self) -> None:
        """Browse to a normal page so Cloudflare hands us its clearance cookies."""
        url = EXPLORE_NOW.format(city=self.city)
        self._log(f"seeding session at {url}")
        self._page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        self._page.wait_for_timeout(3000)          # let any JS challenge settle
        if "cloudflare" in (self._page.title() or "").lower():
            raise FetchError(
                "Cloudflare blocked the session. Checks worth making: the browser must "
                "run headful (use xvfb-run on a server, never headless), and the exit IP "
                "must not be a flagged one -- try a different host, or set a proxy."
            )
        for cookie in self._ctx.cookies():
            if cookie["name"] == "bmsId" and cookie["value"]:
                self._bms_id = cookie["value"].strip('"')

    def _ensure_on_site(self) -> None:
        if not self._page or "bookmyshow.com" not in (self._page.url or ""):
            self._seed()

    def _raw_fetch(self, url: str, headers: Dict[str, str]) -> Tuple[int, str]:
        try:
            res = self._page.evaluate(_JS_FETCH, [url, headers])
        except PlaywrightError as exc:
            raise FetchError(f"browser fetch failed: {exc}") from exc
        return int(res.get("status", 0)), res.get("body", "")

    def _fetch_with_recovery(self, url: str, headers: Dict[str, str]) -> Tuple[int, str]:
        self._ensure_on_site()
        status, body = self._raw_fetch(url, headers)
        if status in (0, 403, 429, 503) or body.lstrip().startswith("<"):
            self._log(f"got HTTP {status} -- re-seeding session and retrying once")
            self._seed()
            status, body = self._raw_fetch(url, headers)
        return status, body

    # -- public API --------------------------------------------------------

    def showtimes(self, event_code: str, date_code: str) -> Dict[str, Any]:
        """Raw showtimes-by-event payload for one movie on one date."""
        params = {
            "appCode": "MOBAND2",
            "appVersion": "14304",
            "language": "en",
            "eventCode": event_code,
            "regionCode": self.region_code,
            "subRegion": self.region_code,
            "bmsId": self._bms_id,
            "token": APP_TOKEN,
            "lat": self.lat,
            "lon": self.lon,
            "query": "",
            "dateCode": date_code,
        }
        url = f"{SHOWTIMES_API}?{urlencode(params)}"
        headers = {
            "x-region-code": self.region_code,
            "x-subregion-code": self.region_code,
            "accept": "application/json",
        }
        status, body = self._fetch_with_recovery(url, headers)
        if status != 200:
            raise FetchError(f"showtimes API returned HTTP {status}")
        try:
            return json.loads(body)
        except ValueError as exc:
            raise FetchError(f"showtimes API returned non-JSON ({body[:120]!r})") from exc

    def page_html(self, url: str) -> str:
        """Fetch a normal page by navigating to it (document requests pass cleanly)."""
        try:
            response = self._page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        except PlaywrightError as exc:
            raise FetchError(f"could not open {url}: {exc}") from exc
        self._page.wait_for_timeout(1500)
        if response is not None and response.status != 200:
            raise FetchError(f"{url} returned HTTP {response.status}")
        return self._page.content()

    def list_movies(self, include_upcoming: bool = True) -> Dict[str, str]:
        """{event_code: slug} for every movie listed in this city."""
        found: Dict[str, str] = {}
        pages = [EXPLORE_NOW.format(city=self.city)]
        if include_upcoming:
            pages.append(EXPLORE_UPCOMING.format(city=self.city))
        pattern = r"/movies/%s/([a-z0-9\-]+)/(ET\d+)" % re.escape(self.city)
        for url in pages:
            try:
                html = self.page_html(url)
            except FetchError as exc:
                self._log(f"could not read {url}: {exc}")
                continue
            hits = re.findall(pattern, html)
            self._log(f"{url}: {len(html)} bytes, {len(hits)} movie links")
            for slug, code in hits:
                found.setdefault(code, slug)
        return found
