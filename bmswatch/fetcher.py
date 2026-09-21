"""BookMyShow clients.

BookMyShow sits behind Cloudflare, which rejects ordinary HTTP clients --
`curl` and Python `requests` both get a 403 even from a residential IP. What
it is actually checking is the **TLS handshake fingerprint**, not cookies,
headers, or JavaScript: a client whose handshake looks like Chrome's is served
normally, with no cookies and no challenge at all.

So the default path (`HTTPSession`) is a plain HTTP client that performs
Chrome's TLS handshake, via curl_cffi. A poll is ~40 ms and a few MB of RAM,
which runs anywhere -- a phone, a Pi, the smallest free VPS tier.

`BrowserSession` is the fallback: a real headful browser driven by Playwright.
It is far heavier (a browser, plus a virtual display on a server) and is only
worth reaching for if the lightweight path ever stops getting through.
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

SHOWTIMES_API = "https://in.bookmyshow.com/api/movies-data/showtimes-by-event"
EXPLORE_NOW = "https://in.bookmyshow.com/explore/movies-{city}"
EXPLORE_UPCOMING = "https://in.bookmyshow.com/explore/upcoming-movies-{city}"

# The public token BookMyShow's own Android app sends. Not a secret, not tied
# to any account -- it only identifies the client to their read-only API.
APP_TOKEN = "67x1xa33b4x422b361ba"

MOVIE_LINK = r"/movies/%s/([a-z0-9\-]+)/(ET\d+)"


class FetchError(RuntimeError):
    pass


def showtimes_url(event_code: str, date_code: str, region: str,
                  bms_id: str, lat: str, lon: str) -> str:
    return SHOWTIMES_API + "?" + urlencode({
        "appCode": "MOBAND2",
        "appVersion": "14304",
        "language": "en",
        "eventCode": event_code,
        "regionCode": region,
        "subRegion": region,
        "bmsId": bms_id,
        "token": APP_TOKEN,
        "lat": lat,
        "lon": lon,
        "query": "",
        "dateCode": date_code,
    })


def region_cookie(city: str, region_code: str, lat: str, lon: str) -> str:
    """The cookie that tells BookMyShow which city's pages to serve."""
    return quote(json.dumps({
        "regionCode": region_code,
        "regionNameSlug": city,
        "regionCodeSlug": region_code.lower(),
        "regionName": city.replace("-", " ").title(),
        "subCode": "",
        "subName": "",
        "Lat": lat,
        "Long": lon,
        "countryCode": "IN",
    }, separators=(",", ":")))


class _BaseSession:
    """Shared config and the parsing that both transports need."""

    def __init__(self, city: str = "hyderabad", region_code: str = "HYD",
                 lat: str = "17.3850", lon: str = "78.4867",
                 verbose: bool = False, **_ignored: Any):
        self.city = city
        self.region_code = region_code
        self.lat = lat
        self.lon = lon
        self.verbose = verbose
        self.bms_id = "1.%d.%d" % (random.randint(10**8, 10**9), int(time.time() * 1000))

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def start(self) -> None: ...

    def close(self) -> None: ...

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"[fetch] {msg}", flush=True)

    def _parse_showtimes(self, body: str) -> Dict[str, Any]:
        try:
            return json.loads(body)
        except ValueError as exc:
            raise FetchError(
                f"showtimes API returned non-JSON ({body[:120]!r})") from exc

    def list_movies(self, include_upcoming: bool = True) -> Dict[str, str]:
        """{event_code: slug} for every movie listed in this city."""
        found: Dict[str, str] = {}
        pages = [EXPLORE_NOW.format(city=self.city)]
        if include_upcoming:
            pages.append(EXPLORE_UPCOMING.format(city=self.city))
        pattern = MOVIE_LINK % re.escape(self.city)
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

    def page_html(self, url: str) -> str:
        raise NotImplementedError

    def showtimes(self, event_code: str, date_code: str) -> Dict[str, Any]:
        raise NotImplementedError


class HTTPSession(_BaseSession):
    """The light path: plain HTTP with a Chrome TLS fingerprint."""

    name = "http"

    def __init__(self, *args, impersonate: str = "chrome",
                 proxy: Optional[dict] = None, timeout: int = 30, **kwargs):
        super().__init__(*args, **kwargs)
        self.impersonate = impersonate
        self.timeout = timeout
        self._proxy = proxy
        self._session = None

    def start(self) -> None:
        try:
            from curl_cffi import requests as cr
        except ImportError as exc:                       # pragma: no cover
            raise FetchError(
                "curl_cffi is not installed -- run: pip install -r requirements.txt"
            ) from exc

        kwargs: Dict[str, Any] = {"impersonate": self.impersonate}
        if self._proxy:
            server = self._proxy.get("server", "")
            user, pwd = self._proxy.get("username"), self._proxy.get("password")
            if user and "://" in server:
                scheme, rest = server.split("://", 1)
                server = f"{scheme}://{user}:{pwd or ''}@{rest}"
            kwargs["proxies"] = {"http": server, "https": server}

        self._session = cr.Session(**kwargs)
        self._session.cookies.set(
            "rgn", region_cookie(self.city, self.region_code, self.lat, self.lon),
            domain=".bookmyshow.com")
        self._log(f"http session ready (impersonating {self.impersonate})")

    def close(self) -> None:
        try:
            if self._session:
                self._session.close()
        except Exception:
            pass
        self._session = None

    def _get(self, url: str, headers: Optional[Dict[str, str]] = None) -> Tuple[int, str]:
        if self._session is None:
            self.start()
        try:
            res = self._session.get(url, headers=headers or {}, timeout=self.timeout)
        except Exception as exc:
            raise FetchError(f"request failed: {exc}") from None
        return res.status_code, res.text

    def showtimes(self, event_code: str, date_code: str) -> Dict[str, Any]:
        url = showtimes_url(event_code, date_code, self.region_code,
                            self.bms_id, self.lat, self.lon)
        status, body = self._get(url, {
            "x-region-code": self.region_code,
            "x-subregion-code": self.region_code,
            "accept": "application/json",
        })
        if status != 200:
            raise FetchError(f"showtimes API returned HTTP {status}")
        return self._parse_showtimes(body)

    def page_html(self, url: str) -> str:
        status, body = self._get(url, {"accept": "text/html"})
        if status != 200:
            raise FetchError(f"{url} returned HTTP {status}")
        return body

    def probe(self) -> bool:
        """Cheap check that Cloudflare is letting this client through."""
        try:
            html = self.page_html(EXPLORE_NOW.format(city=self.city))
        except FetchError as exc:
            self._log(f"probe failed: {exc}")
            return False
        return bool(re.search(MOVIE_LINK % re.escape(self.city), html))


# --------------------------------------------------------------------------
# Fallback: a real browser. Heavier, but a different fingerprint entirely.
# --------------------------------------------------------------------------

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


class BrowserSession(_BaseSession):
    """The heavy path: a headful browser. Headless is always blocked."""

    name = "browser"

    def __init__(self, *args, profile_dir: Optional[Path] = None,
                 channel: str = "chrome", window: str = "minimized",
                 proxy: Optional[dict] = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.profile_dir = Path(profile_dir) if profile_dir else Path(".chrome-profile")
        self.channel = channel
        self.window = window
        self.proxy = proxy
        self._pw = None
        self._ctx = None
        self._page = None

    def start(self) -> None:
        if self._ctx is not None:
            return                       # make_session may have started us already
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise FetchError(
                "the browser fallback needs Playwright -- run: pip install playwright "
                "&& python -m playwright install chromium") from exc

        self._pw = sync_playwright().start()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            try:
                (self.profile_dir / lock).unlink()
            except (OSError, FileNotFoundError):
                pass

        args = list(CHROME_ARGS)
        if os.environ.get("BMS_NO_SANDBOX") == "1":
            args += ["--no-sandbox", "--disable-dev-shm-usage"]
        if self.window == "offscreen":
            args += ["--window-position=-3000,-3000", "--window-size=1000,700"]

        last_error: Optional[Exception] = None
        for channel in self._channel_candidates():
            try:
                kwargs: Dict[str, Any] = dict(
                    user_data_dir=str(self.profile_dir), headless=False, args=args,
                    ignore_default_args=["--enable-automation"],
                    locale="en-IN", timezone_id="Asia/Kolkata", viewport=None)
                if channel:
                    kwargs["channel"] = channel
                if self.proxy:
                    kwargs["proxy"] = self.proxy
                self._ctx = self._pw.chromium.launch_persistent_context(**kwargs)
                self._log(f"launched {channel or 'bundled chromium'}")
                break
            except Exception as exc:
                last_error = exc
                self._log(f"channel {channel!r} unavailable: {str(exc)[:100]}")
        if self._ctx is None:
            raise FetchError(f"could not launch a browser: {last_error}")

        self._ctx.add_init_script(_STEALTH)
        self._ctx.add_cookies(self._region_cookies())
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        self._minimise()
        self._seed()

    def _channel_candidates(self) -> List[Optional[str]]:
        if self.channel.lower() in ("", "chromium", "bundled", "none"):
            return [None]
        order: List[Optional[str]] = [self.channel]
        for fallback in ("chrome", "chrome-beta", "msedge", None):
            if fallback not in order:
                order.append(fallback)
        return order

    def _region_cookies(self) -> List[dict]:
        common = {"domain": ".bookmyshow.com", "path": "/"}
        return [
            {"name": "rgn",
             "value": region_cookie(self.city, self.region_code, self.lat, self.lon),
             **common},
            {"name": "_region_code", "value": self.region_code, **common},
            {"name": "_region_slug", "value": self.city, **common},
        ]

    def _minimise(self) -> None:
        if self.window != "minimized":
            return
        try:
            cdp = self._ctx.new_cdp_session(self._page)
            window_id = cdp.send("Browser.getWindowForTarget")["windowId"]
            cdp.send("Browser.setWindowBounds",
                     {"windowId": window_id, "bounds": {"windowState": "minimized"}})
        except Exception as exc:
            self._log(f"could not minimise window: {exc}")

    def _seed(self) -> None:
        url = EXPLORE_NOW.format(city=self.city)
        self._page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        self._page.wait_for_timeout(3000)
        if "cloudflare" in (self._page.title() or "").lower():
            raise FetchError(
                "Cloudflare blocked the browser session. It must run headful "
                "(use xvfb-run on a server, never headless).")
        for cookie in self._ctx.cookies():
            if cookie["name"] == "bmsId" and cookie["value"]:
                self.bms_id = cookie["value"].strip('"')

    def close(self) -> None:
        for closer in (self._ctx, self._pw):
            try:
                if closer:
                    closer.close() if closer is self._ctx else closer.stop()
            except Exception:
                pass
        self._pw = self._ctx = self._page = None

    def _raw_fetch(self, url: str, headers: Dict[str, str]) -> Tuple[int, str]:
        if not self._page or "bookmyshow.com" not in (self._page.url or ""):
            self._seed()
        res = self._page.evaluate(_JS_FETCH, [url, headers])
        status, body = int(res.get("status", 0)), res.get("body", "")
        if status in (0, 403, 429, 503) or body.lstrip().startswith("<"):
            self._log(f"got HTTP {status} -- re-seeding and retrying once")
            self._seed()
            res = self._page.evaluate(_JS_FETCH, [url, headers])
            status, body = int(res.get("status", 0)), res.get("body", "")
        return status, body

    def showtimes(self, event_code: str, date_code: str) -> Dict[str, Any]:
        url = showtimes_url(event_code, date_code, self.region_code,
                            self.bms_id, self.lat, self.lon)
        status, body = self._raw_fetch(url, {
            "x-region-code": self.region_code,
            "x-subregion-code": self.region_code,
            "accept": "application/json",
        })
        if status != 200:
            raise FetchError(f"showtimes API returned HTTP {status}")
        return self._parse_showtimes(body)

    def page_html(self, url: str) -> str:
        response = self._page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        self._page.wait_for_timeout(1500)
        if response is not None and response.status != 200:
            raise FetchError(f"{url} returned HTTP {response.status}")
        return self._page.content()


def make_session(engine: str = "auto", **kwargs) -> _BaseSession:
    """Build a session. 'auto' uses HTTP, falling back to a browser if blocked.

    Browser-only options are dropped when the HTTP transport is chosen, so one
    config can describe both.
    """
    browser_only = ("profile_dir", "channel", "window")
    http_kwargs = {k: v for k, v in kwargs.items() if k not in browser_only}
    engine = (engine or "auto").lower()

    if engine == "browser":
        return BrowserSession(**kwargs)

    session = HTTPSession(**http_kwargs)
    if engine == "http":
        return session

    session.start()
    if session.probe():
        return session
    session.close()
    print("[fetch] plain HTTP was blocked -- falling back to a browser", flush=True)
    return BrowserSession(**kwargs)


# Kept so older scripts importing the original name still work.
BMSSession = BrowserSession
