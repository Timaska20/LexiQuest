#!/usr/bin/env bash
set -euo pipefail

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker не найден. Установите Docker Engine + Compose plugin и запустите скрипт снова."
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "docker compose plugin не найден."
  exit 1
fi

mkdir -p data media/videos media/audio media/tmp dictionaries exports

if [ ! -f .env ]; then
  PASS="$(openssl rand -hex 10 2>/dev/null || python3 - <<'PY'
import secrets
print(secrets.token_hex(10))
PY
)"
  cat > .env <<ENV
LEXIQUEST_PORT=8088
APP_USER=lexi
APP_PASSWORD=${PASS}
MEDIA_ROOT=/app/media
DATA_DIR=/app/data
DICTIONARY_DIR=/app/dictionaries
EXPORT_DIR=/app/exports
MAX_UPLOAD_MB=2048
MAX_DICTIONARY_UPLOAD_MB=1024
VOT_BRIDGE_URL=http://vot:3100
VOT_JS_VERSION=latest
VOT_POLL_TIMEOUT_MS=180000
VOT_POLL_INTERVAL_MS=5000
VOT_WORKER_HOST=vot-new.toil-dump.workers.dev
ENV
fi

docker compose up -d --build

PORT="$(grep '^LEXIQUEST_PORT=' .env | cut -d= -f2-)"
USER="$(grep '^APP_USER=' .env | cut -d= -f2-)"
PASS="$(grep '^APP_PASSWORD=' .env | cut -d= -f2-)"

echo
echo "LexiQuest Cake запущен на localhost VPS (безопаснее для первого теста)."
echo "Логин: ${USER}"
echo "Пароль: ${PASS}"
echo
echo "На телефоне открой НОВУЮ сессию Termux и держи туннель запущенным:"
echo "  ssh -N -L ${PORT}:127.0.0.1:${PORT} USER@YOUR_VPS_IP"
echo
echo "После этого открой в браузере телефона:"
echo "  http://127.0.0.1:${PORT}"
echo
echo "Проверка: docker compose ps"
echo "Логи:     docker compose logs -f --tail=100"
