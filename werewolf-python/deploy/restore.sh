#!/bin/sh
set -eu
: "${DATABASE_URL:?DATABASE_URL must be set}"
SRC="${1:?usage: restore.sh <dump-file>}"
pg_restore --clean --if-exists -d "$DATABASE_URL" "$SRC"
