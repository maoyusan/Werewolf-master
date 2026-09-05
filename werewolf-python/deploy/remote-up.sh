#!/usr/bin/env bash
set -euo pipefail
ROOT=/opt/werewolf-python
cd "$ROOT"

mkdir -p deploy/napcat/config deploy/napcat/ntqq
if [ ! -f deploy/napcat/config/onebot11.json ] && [ -f deploy/napcat-config-onebot11.json ]; then
  cp deploy/napcat-config-onebot11.json deploy/napcat/config/onebot11.json
fi

if [ ! -f .env ]; then
  PW="$(openssl rand -hex 16)"
  umask 077
  cat > .env <<EOF
NAPCAT_WS_URL=ws://napcat:3001
NAPCAT_ACCESS_TOKEN=
POSTGRES_PASSWORD=${PW}
GAME_MODE=Normal
ADMIN_USER_IDS=
DEV_USER_IDS=
LOG_LEVEL=INFO
MIN_PLAYERS=5
EOF
  echo "wrote ${ROOT}/.env"
fi

docker compose --env-file .env -p werewolf -f deploy/docker-compose.yml up -d --build

echo '--- health ---'
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
  if curl -fsS http://127.0.0.1:18100/healthz >/dev/null 2>&1; then
    curl -fsS http://127.0.0.1:18100/healthz
    echo
    break
  fi
  sleep 3
done
curl -fsS http://127.0.0.1:18100/readyz || true
echo
echo '--- napcat logs (QR / webui token) ---'
docker logs napcat --tail 80 || true
