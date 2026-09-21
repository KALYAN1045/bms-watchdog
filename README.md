# bms-watchdog

Pings your phone the moment BookMyShow opens tickets for a movie at the
theatres you care about — and keeps pinging until you tap **✅ Got it**.

```
BookMyShow  →  showtimes API  →  filter by theatre / date / format / seats
                                              ↓
                      Telegram  ←  🚨 TICKETS OPEN · Prasads · 8:00 AM · PCX
                      (repeats until you acknowledge)
```

It is a small Python process — no browser, ~40 ms and a few MB per check — so
it runs anywhere that stays on: a free VPS tier, a Raspberry Pi, or an Android
phone in your pocket. Free to build and free to run.

---

## How it gets past Cloudflare

BookMyShow is behind Cloudflare, and the obvious approaches fail. I tested
each against the live site rather than guessing, and the result was not what
the usual advice online says:

| Client | Result |
|---|---|
| `curl` / Python `requests`, from a residential Indian IP | 403 |
| Playwright **headless** — Firefox, Chromium, Chrome headless shell | 403 |
| Playwright **headful** — real Chrome, bundled Chromium | 200 ✅ |
| **Plain HTTP with a Chrome TLS fingerprint** (`curl_cffi`) | **200 ✅** |
| Same library with impersonation switched off | 403 |

That last pair is the whole story. Cloudflare is fingerprinting the **TLS
handshake** — not cookies, not headers, not JavaScript. No challenge is ever
issued and no clearance cookie is needed. A client whose handshake looks like
Chrome's is simply served, and one that doesn't is refused.

So the watchdog sends an ordinary HTTPS request that happens to negotiate TLS
exactly the way Chrome does, then calls BookMyShow's own mobile showtimes API
— the same endpoint their Android app uses. That's it.

A headful-browser fallback is still included (`engine: browser`) in case that
ever stops working, but it is not the normal path and you will probably never
need it.

---

## 1. Configure it (10 minutes)

```bash
cd bms-watchdog
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

### Make a Telegram bot

1. In Telegram, message **@BotFather** → `/newbot` → pick a name (the username
   must end in `bot`). It replies with a token like `8012345678:AAE...`.
2. Paste that token into `.env`:

   ```
   TELEGRAM_BOT_TOKEN=8012345678:AAE...
   ```

3. Run the setup command:

   ```bash
   ./venv/bin/python watch.py telegram-setup
   ```

   It validates the token, waits for you to press **Start** in Telegram,
   discovers your chat id, writes it to `.env`, and sends a confirmation.
   Your token is never printed — not even inside error messages.

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

## 2. Run it somewhere that stays on

Because there's no browser, the footprint is tiny — roughly 40 MB of RAM and a
fraction of a CPU. Anything works.

### Your Android phone, via Termux

Genuinely viable now, and free. One wrinkle: `curl_cffi` ships Linux (glibc)
wheels, and Termux is Android/bionic — so install inside a Debian container,
which `proot-distro` makes a one-liner. No root needed.

```bash
# in Termux
pkg update -y && pkg install -y proot-distro
proot-distro install debian
proot-distro login debian
```
```bash
# now inside Debian
apt update && apt install -y python3 python3-venv git
git clone <your-repo> ~/bms-watchdog && cd ~/bms-watchdog
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
# copy your .env and watchlist.yaml across, then:
./venv/bin/python watch.py doctor
nohup ./venv/bin/python watch.py run >> watchdog.log 2>&1 &
```

Three things Android will otherwise do to you:

* **Doze will suspend it.** Install Termux:API (`pkg install termux-api`) and
  run `termux-wake-lock` before starting.
* **Battery optimisation will kill it.** Android Settings → Apps → Termux →
  Battery → *Unrestricted*.
* **A reboot ends it.** Install the **Termux:Boot** addon and put a startup
  script in `~/.termux/boot/`.

Keep the phone charging — a poll every 60 s is light, but 24/7 wakelock isn't
free. An old spare phone plugged in on a shelf is the ideal version of this.

I could not test the Termux path from your Mac, so treat these steps as
carefully-researched rather than verified — `watch.py doctor` will confirm it
in one command on the phone.

### A VPS or Raspberry Pi

```bash
bash scripts/deploy.sh user@your-server
```

Syncs the folder, builds the image, runs `doctor` as a gate, and starts it
detached. The image is now plain `python:3.12-slim`. A Pi at home is the
lowest-risk option because it's a residential IP; Oracle Cloud's Always Free
tier is the ₹0 cloud option and the image builds on its ARM instances.

Or without Docker at all — it's just a Python process:

```bash
./venv/bin/python watch.py run
```

### Your Mac

```bash
bash scripts/install-launchd.sh
```

Starts at login, restarts on crash, logs to `logs/watchdog.log`. Only runs
while the Mac is awake — closing the lid stops it. Undo with
`launchctl unload ~/Library/LaunchAgents/com.bmswatch.watchdog.plist`.

### GitHub Actions

`.github/workflows/watch.yml` runs a check on a schedule for free. The catch
is timing, not access: GitHub's cron has a 5-minute floor and is routinely
5–20 minutes late. A backstop, not a way to win a first-day scramble.

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
`ok`). Each distinct show alerts only once, ever — when more shows open later,
only the new ones ping you.

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
`heartbeat_minutes`, `city`, `region_code`, `engine`, `proxy`, and the
browser-fallback options `channel` and `window`.

Environment overrides, so one watchlist serves every machine: `BMS_ENGINE`,
`BMS_PROXY`, `BMS_CHANNEL`, `BMS_WINDOW`, and `BMS_DESKTOP=0` to mute desktop
banners.

Watching a film that isn't on BookMyShow yet? Leave out `event_code`. The
watchdog scans the now-showing *and* upcoming pages each cycle, picks up the
`ET…` code the moment the page appears, and starts polling it.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| Everything 403s | Your IP may be rated badly. Try another host, or set `proxy:`. As a last resort set `engine: browser` (needs Playwright + a headful browser) |
| `curl_cffi is not installed` | `pip install -r requirements.txt`. On Termux, use the `proot-distro` route above — native Termux has no wheel for it |
| Telegram says nothing | You must message the bot first. Re-run `watch.py telegram-setup` |
| `no channel configured` | `source .env` first, or check `env_file: .env` in Docker |
| Status stays `🙈 none match your filters` | Filters too strict. Run `watch.py theatres -e ET…` and copy names exactly |
| Status stays `🔍 not listed` | No BookMyShow page in this city yet. That's the correct answer — it will flip on its own |
| Want to re-test alerts | `rm state.json` clears the "already alerted" memory |

Add `-v` to any command for transport-level detail.

---

## Tests

```bash
./venv/bin/python tests.py
```

No network needed. They cover parsing a real API payload, whole-word theatre
matching, sold-out detection, the substitute-date trap (BookMyShow answers a
date that has no shows by returning the *next* date that does — the parser
rejects that, or you'd be alerted for the wrong day), config validation,
de-duplication across restarts, HTML escaping, and the Telegram client's
timeout and token-redaction behaviour.

---

## Please keep it polite

This reads the same public listing data your browser does, about once a
minute. Don't drop `poll_seconds` to a few seconds across a dozen watches —
that is what gets IP ranges blocked and ruins it for everyone. The watchdog
refuses anything under 20 seconds and adds random jitter.

It **watches and notifies only**. It never logs into your account, never
touches payment, and never books. Booking stays you, tapping a link.
