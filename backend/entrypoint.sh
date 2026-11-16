#!/bin/sh
set -e

# Apply migrations, create the admin account and (if SEED_DEMO_DATA is true)
# the demo data, then serve. Both steps are safe to repeat.
alembic upgrade head
python -m app.seed

# ROOT_PATH is the prefix nginx strips ("/api"), so the docs build correct URLs.
#
# The API is only reachable through nginx on Docker's private network, so
# X-Forwarded-* headers are trusted from private addresses and from nowhere else.
#
# --limit-concurrency: beyond this many requests at once, answer 503 at once
# instead of queueing without bound. A fast "try again" beats a hung page.
#
# --timeout-graceful-shutdown: on a deploy, let requests in flight finish.
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 \
    --root-path "${ROOT_PATH:-}" \
    --proxy-headers \
    --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-127.0.0.1,172.16.0.0/12,10.0.0.0/8,192.168.0.0/16}" \
    --limit-concurrency "${MAX_CONCURRENT_REQUESTS:-500}" \
    --timeout-graceful-shutdown 25
