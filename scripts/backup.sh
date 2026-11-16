#!/bin/sh
# Back up the database to a compressed dump and drop dumps older than 14 days.
#
#   scripts/backup.sh [directory]        (default: ./backups)
#
# Run it from cron on the server (see DEPLOY.md). To restore a dump:
#
#   docker compose cp backups/<file>.dump db:/tmp/restore.dump
#   docker compose exec -T db sh -c \
#     'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists /tmp/restore.dump'
set -eu

cd "$(dirname "$0")/.."
directory="${1:-backups}"
mkdir -p "$directory"
target="$directory/krunchbox-$(date -u +%Y%m%dT%H%M%SZ).dump"

# The dump is written inside the container and then copied out, rather than
# piped through the terminal, so the binary file arrives intact on any host.
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -f /tmp/backup.dump'
docker compose cp db:/tmp/backup.dump "$target"
docker compose exec -T db sh -c 'pg_restore --list /tmp/backup.dump > /dev/null && rm /tmp/backup.dump'

find "$directory" -name 'krunchbox-*.dump' -mtime +14 -delete
echo "Backup written to $target"
