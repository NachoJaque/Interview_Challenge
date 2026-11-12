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
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 \
    --root-path "${ROOT_PATH:-}" \
    --proxy-headers \
    --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-127.0.0.1,172.16.0.0/12,10.0.0.0/8,192.168.0.0/16}"
