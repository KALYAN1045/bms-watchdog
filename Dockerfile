# Small image: no browser, no virtual display. The watchdog gets past
# Cloudflare with a Chrome TLS fingerprint over plain HTTP, so all it needs
# is Python. Works on amd64 and arm64.
FROM python:3.12-slim-bookworm

ENV TZ=Asia/Kolkata \
    PYTHONUNBUFFERED=1 \
    BMS_DESKTOP=0

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python", "watch.py", "bot"]
