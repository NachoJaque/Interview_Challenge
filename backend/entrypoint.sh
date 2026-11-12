#!/bin/sh
set -e

# Apply migrations, load demo data (idempotent), then serve.
alembic upgrade head
python -m app.seed
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers
