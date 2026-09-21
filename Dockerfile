# Runs the watchdog on any Linux box (amd64 or arm64).
#
# The one thing that matters: Chromium must run HEADFUL or Cloudflare blocks it.
# There is no display on a server, so we give it a virtual one with Xvfb.
FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Kolkata \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    BMS_NO_SANDBOX=1 \
    BMS_CHANNEL=chromium \
    BMS_WINDOW=visible \
    BMS_DESKTOP=0

WORKDIR /app

COPY requirements.txt .
RUN apt-get update \
 && apt-get install -y --no-install-recommends xvfb tini tzdata ca-certificates \
 && pip install --no-cache-dir -r requirements.txt \
 && python -m playwright install --with-deps chromium \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

COPY . .

# Xvfb gives Chromium a display; tini reaps the browser processes it spawns.
ENTRYPOINT ["/usr/bin/tini", "--", "xvfb-run", "--auto-servernum", \
            "--server-args=-screen 0 1280x1024x24"]
CMD ["python", "watch.py", "run"]
