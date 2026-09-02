#!/bin/sh
set -eu
: "${DATABASE_URL:?DATABASE_URL must be set}"
DEST="${1:-/var/backups/werewolf/werewolf-$(date +%Y%m%d-%H%M%S).dump}"
mkdir -p "$(dirname "$DEST")"
pg_dump "$DATABASE_URL" -Fc -f "$DEST"
echo "$DEST"
