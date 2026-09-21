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

The process needs roughly **40 MB of RAM** and a fraction of a CPU. That fits
inside free tiers that are free *forever*, not free-for-12-months.

### Google Cloud Always Free — recommended

One `e2-micro` instance, free with no end date, and — unlike Oracle — no
policy of reclaiming instances for being idle. That matters here, because a
watchdog polling once a minute *is* idle by any CPU measure.

| | |
|---|---|
| Cost | ₹0/month, no expiry |
| Machine | `e2-micro` — 2 vCPU burst, 1 GB RAM, 30 GB disk |
| Regions | `us-west1` (Oregon), `us-central1` (Iowa), `us-east1` (S. Carolina) |
| Egress | 1 GB/month outbound. This uses roughly 45 MB |
| Catch | US datacenter IP, and a card is needed for identity verification |

1. Sign up at `cloud.google.com/free`, create a project.
2. Compute Engine → Create instance → machine type **e2-micro**, region one of
   the three above, boot disk **Ubuntu 22.04**, disk size 30 GB.
3. Add your SSH key under Security → Manage Access.
4. From your Mac:

```bash
bash scripts/deploy.sh <user>@<external-ip>
```

That syncs the folder, creates a venv, runs `doctor` **as a gate**, then
installs a systemd service that starts at boot and restarts on crash. No
Docker, no browser — about 40 MB resident.

```bash
ssh <user>@<ip> 'journalctl -u bms-watchdog -f'      # live logs
ssh <user>@<ip> 'sudo systemctl restart bms-watchdog'
```

Re-run `deploy.sh` any time you change the watchlist.

### Oracle Cloud Always Free — Indian IP, but read this first

Oracle is the only provider offering a permanently free VM in **Mumbai or
Hyderabad**, which is appealing: a local IP is the least likely to be rated
badly by Cloudflare. Two things to understand before choosing it.

**Signing up starts a 30-day trial *and* grants Always Free resources.** They
coexist. When the trial ends the credits expire but Always Free resources keep
running — *provided* you chose a shape marked **"Always Free eligible"**
(`VM.Standard.E2.1.Micro`). Pick anything else and it dies on day 30.

**Oracle reclaims idle Always Free instances.** If, across 7 days, CPU
(95th percentile) is under 20% *and* network is under 20%, the instance is
reclaimed. This watchdog sits near 0% CPU, so it would very likely qualify.
Your options are to upgrade the account to Pay As You Go — Always Free
resources stay free and reclamation no longer applies, but a card is then
liable for anything outside the free limits — or to use Google Cloud instead.
Manufacturing fake load to stay above the threshold is not a solution worth
having; it burns a free resource to no purpose.

### Any other Linux box

The deploy script has no Oracle-specific parts — a Raspberry Pi at home, an
old laptop running Linux, or a ₹350/month Hetzner VPS all work identically.
A Pi is the best of all for Cloudflare, because it's a residential IP.

There's also a `Dockerfile` and `docker-compose.yml` if you'd rather run it
containerised, though on a 1 GB box systemd is the lighter choice.

### GitHub Actions — zero signup, but laggy

`.github/workflows/watch.yml` runs on GitHub's free minutes with no card at
all. The catch is timing: GitHub's cron has a 5-minute floor and is routinely
5–20 minutes late. Fine as a backstop or for a quiet re-release; not for a
first-day-first-show scramble.

### Your Mac

```bash
bash scripts/install-launchd.sh
```

Starts at login, restarts on crash. Only runs while the Mac is awake — closing
the lid stops it.

### Android / Termux — possible, but I'd skip it

It would run, but it's the one option I could not verify, and the failure mode
is silent. `curl_cffi` publishes Android wheels, but only under CPython's
official `android_*` platform tags; Termux's Python reports `linux_aarch64`
instead, so pip probably won't match them and will attempt a source build that
needs a C library with no Termux build. The workaround is a Debian container
via `proot-distro`, where the normal Linux wheels apply:

```bash
pkg install -y proot-distro && proot-distro install debian
proot-distro login debian
# then the ordinary Linux install
```

Even when that works, Android will fight you: Doze suspends the process
(needs `termux-wake-lock`), battery optimisation kills it (set Termux to
*Unrestricted*), and a reboot ends it (needs the Termux:Boot addon). For
something whose whole job is firing reliably at an unpredictable minute, a
free VM is a much better bet.

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
