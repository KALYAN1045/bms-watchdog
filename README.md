# bms-watchdog

Pings your phone the moment BookMyShow opens tickets for a movie at the
theatres you care about — and keeps pinging until you tap **✅ Got it**.

```
BookMyShow  →  Chromium (headful, on a virtual display)  →  showtimes API  →  filter
                                                                                ↓
                                   Telegram  ←  🚨 TICKETS OPEN · Prasads · 8:00 AM
                                   (repeats until you acknowledge)
```

Runs on a small Linux server so your laptop can stay shut. Everything it uses
is free or nearly free: Python, a Telegram bot, Chromium. No ScraperAPI, no
paid scraping proxy, no BookMyShow account.

---

## Read this first: the one constraint that shapes everything

BookMyShow sits behind Cloudflare. I tested this against the live site before
writing the code:

| How you ask | Result |
|---|---|
| `curl` / Python `requests` | **403**, every time |
| Playwright **headless** — Firefox, Chromium, Chrome headless shell | **403**, every time |
| Playwright **headful** — real Chrome | **200 ✅** |
| Playwright **headful** — bundled Chromium | **200 ✅** (was blocked on one earlier attempt) |

**Headful is the hard requirement.** Not the specific browser — the fact that
it isn't headless. Servers have no display, so the Docker image gives Chromium
a virtual one with Xvfb. That is the whole trick.

Once a session is warm, each poll is a single ~20 KB JSON call to BookMyShow's
own mobile API from inside that page, which is why checking every 60 seconds
is cheap and unobtrusive.

**One thing I could not test from here: your server's IP.** Cloudflare scores
IP reputation separately from browser fingerprint, and datacenter ranges score
worse than home broadband. `python watch.py doctor` answers this on your server
in about 30 seconds — run it before trusting the setup. If that host is
blocked, the fixes are in [Troubleshooting](#troubleshooting).

---

## 1. Configure it locally (10 minutes)

Do this on your Mac first — it is much easier to pick theatres and test alerts
with a terminal in front of you. Then ship the same folder to the server.

```bash
cd bms-watchdog
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

On your Mac it drives your installed Chrome with a **separate throwaway
profile** in `.chrome-profile/` — it never touches your real browser, logins,
or tabs.

### Make a Telegram bot

1. In Telegram, message **@BotFather** → `/newbot` → pick a name (the username
   must end in `bot`). It replies with a token like `8012345678:AAE...`.
2. Paste that token into `.env` in this folder:

   ```
   TELEGRAM_BOT_TOKEN=8012345678:AAE...
   ```

3. Let the setup command do the rest:

   ```bash
   ./venv/bin/python watch.py telegram-setup
   ```

   It validates the token, waits for you to press **Start** in Telegram,
   discovers your chat id, writes it to `.env`, and sends a confirmation
   message. Your token is never printed — not even inside error messages.

### Find your movie and your theatres

```bash
./venv/bin/python watch.py movies -q paradise
```
```
ET00436621   the-paradise
```
```bash
./venv/bin/python watch.py theatres -e ET00436621 -d 2026-09-25
```
```
AAA Cinemas: Ameerpet          [2D LASER DOLBY ATMOS]
AMB Cinemas: Gachibowli        [2D, IMAX 2D]
Prasads Multiplex: Hyderabad   [2D PCX SCREEN]
...
```

Copy any part of a name into `theatres:`. Matching is case-insensitive and
**whole-word**, so `AMB` finds *AMB Cinemas* but not *Bh**ramaramba***.

### Write your watchlist

```bash
cp watchlist.example.yaml watchlist.yaml
```

```yaml
watches:
  - id: paradise-prasads
    movie: The Paradise
    event_code: ET00436621
    dates: [2026-09-25]
    theatres: [Prasads, AMB Cinemas]
    formats: [IMAX, PCX]      # empty = any format
    languages: [Telugu]
```

### Prove it works

```bash
source .env
./venv/bin/python watch.py doctor       # config + Telegram + live BookMyShow check
./venv/bin/python watch.py test-alert   # see exactly what an alert looks like
```

---

## 2. Put it on a server

Any Linux box with **2 GB RAM** and Docker. Chromium is the heavy part; 1 GB
works but leaves no headroom.

Good options, cheapest first:

| Host | Cost | Notes |
|---|---|---|
| **Oracle Cloud Always Free** | ₹0 forever | 4 ARM cores / 24 GB. The image builds on arm64 |
| **Hetzner CX22** | ~₹350/mo | Simple, fast, x86 |
| **DigitalOcean / Vultr** | ~₹500/mo | Same idea |
| **A Raspberry Pi at home** | ₹0 if you own one | Best IP reputation — it's residential |

```bash
# on the server
sudo apt update && sudo apt install -y docker.io docker-compose-v2 git
git clone <your-repo> bms-watchdog && cd bms-watchdog

# copy over the watchlist.yaml and .env you just tested — unchanged.
# The image sets BMS_CHANNEL=chromium, BMS_WINDOW=visible and BMS_DESKTOP=0
# for you, so the same file works on both machines.

docker compose build
docker compose run --rm watchdog python watch.py doctor   # ← the IP test
docker compose up -d
```

`doctor` is the moment of truth. If it prints `ok — browser session live`,
you're done. If it prints `Cloudflare blocked the session`, that host's IP is
the problem — see [Troubleshooting](#troubleshooting).

```bash
docker compose logs -f          # watch it work
docker compose restart          # after editing watchlist.yaml
docker compose down             # stop
```

It restarts on crash and on server reboot (`restart: unless-stopped`), and
`state.json` lives on the host so a restart never re-sends old alerts.

### Or keep it on the Mac

```bash
bash scripts/install-launchd.sh
```

Starts at login, restarts if it crashes, logs to `logs/watchdog.log`. Chrome
runs minimised and invisible. Stop it with
`launchctl unload ~/Library/LaunchAgents/com.bmswatch.watchdog.plist`.

### What about GitHub Actions?

`.github/workflows/watch.yml` is included, but be clear-eyed about it: GitHub's
cron has a 5-minute floor and is routinely 5–20 minutes late, and its runners
are datacenter IPs. It is a backstop, not a way to win a first-day-first-show
scramble. Add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` as repository secrets
and commit your `watchlist.yaml`.

---

## What an alert looks like

```
🚨 TICKETS OPEN — The Paradise

📅 Friday, 25 Sep 2026
📍 Prasads Multiplex: Hyderabad
   • 08:00 AM · 2D · PCX SCREEN · Telugu — ₹190–450 · 39 seats
   • 11:30 AM · EPIQ · Telugu — ₹450 · 112 seats
📍 AMB Cinemas: Gachibowli
   • 09:30 AM · IMAX 2D · Telugu — ₹390–449 · 294 seats

[ 🎟 Book now ]  [ ✅ Got it ]
```

It repeats every 30 s, up to 8 times, until you tap **✅ Got it** (or reply
`ok`). Tune `repeat_count` / `repeat_every_seconds`. Each distinct show alerts
only once, ever — when more shows open later, only the new ones ping you.

Typical log output:

```
19:23:15 ⏳ The Paradise: listed, booking not open yet
19:24:15 ⏳ The Paradise: listed, booking not open yet
19:25:16 🎟 The Paradise: 12 matching shows (12 new)
```

---

## Watchlist reference

| Key | Meaning |
|---|---|
| `movie` | Display name, used in alerts |
| `event_code` | BookMyShow's `ET…` code. Omit it and the watchdog finds it by name |
| `search` | Slug to search when there is no `event_code`, e.g. `avengers-endgame-encore` |
| `dates` | `[2026-09-25, 2026-09-26]`. Omit to watch every bookable date |
| `next_days` | Alternative: watch today + N days |
| `max_dates` | Cap on dates polled when `dates` is omitted (default 5) |
| `theatres` | Whole-word substrings, any match wins. Empty = all theatres |
| `exclude_theatres` | Never alert for these |
| `formats` | `[IMAX, EPIQ, PCX, 4DX, DOLBY]`. Matches screen format *or* attribute |
| `languages` | `[Telugu, Hindi]` |
| `require_seats` | `true` = only alert when seats are actually free (seat sniping) |
| `min_seats` | Minimum free seats when `require_seats` is on |
| `time_between` | `["18:00", "23:59"]` — evening shows only |
| `enabled` | `false` parks a watch without deleting it |

Global `defaults:` take `poll_seconds` (minimum 20), `jitter_seconds`,
`heartbeat_minutes` (periodic "still alive" ping), `city`, `region_code`,
`channel`, `window`, and `proxy`.

The last four can be overridden per-machine with environment variables, which
is how one watchlist serves both your Mac and the container:
`BMS_CHANNEL`, `BMS_WINDOW`, `BMS_PROXY`, and `BMS_DESKTOP=0` to mute desktop
banners.

Watching a film that isn't on BookMyShow yet? Leave out `event_code`. The
watchdog scans the now-showing *and* upcoming pages each cycle, picks up the
`ET…` code the moment the page appears, and starts polling it.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Cloudflare blocked the session` **on a server** | Confirm Chromium is headful — the container must run under `xvfb-run`, never `--headless`. If it still fails, it's the IP: try a different provider, move to a Pi at home, or set `proxy:` in `defaults:` |
| `Cloudflare blocked the session` **on the Mac** | Quit Cloudflare WARP / ProtonVPN and retry |
| Container exits immediately | `docker compose logs`. Usually a YAML error in `watchlist.yaml` — `doctor` names the line |
| Chromium crashes in Docker | Keep `shm_size: "1gb"` in `docker-compose.yml` |
| Telegram says nothing | You must message the bot first. Re-run `doctor` |
| `no channel configured` | The container isn't seeing `.env` — check `env_file: .env` and that `.env` exists on the server |
| Status stays `🙈 none match your filters` | Filters are too strict. Run `watch.py theatres -e ET…` and copy names exactly |
| Status stays `🔍 not listed` | The movie has no BookMyShow page in this city yet. That's the correct answer — it will flip on its own |
| Want to re-test alerts | `rm state.json` clears the "already alerted" memory |

Add `-v` to any command for browser-level detail.

---

## Tests

```bash
./venv/bin/python tests.py
```

20 tests, no network. They cover parsing a real API payload, whole-word theatre
matching, sold-out detection, the substitute-date trap (BookMyShow answers a
date that has no shows by returning the *next* date that does — the parser
rejects that, or you'd get alerted for the wrong day), config validation,
de-duplication across restarts, and HTML escaping.

---

## Please keep it polite

This reads the same public listing data your browser does, about once a
minute. Don't drop `poll_seconds` to a few seconds across a dozen watches —
that is what gets IP ranges blocked and ruins it for everyone. The watchdog
refuses anything under 20 seconds and adds random jitter.

It **watches and notifies only**. It never logs into your account, never
touches payment, and never books. Booking stays you, tapping a link.
