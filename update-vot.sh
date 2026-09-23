#!/usr/bin/env bash
set -euo pipefail

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker не найден"
  exit 1
fi

printf '%s\n' "Обновляю только VOT bridge до свежего @vot.js/node..."
docker compose build --pull --no-cache vot
docker compose up -d vot
printf '%s' "vot.js version: "
docker compose exec -T vot cat /app/vot-version.txt
printf '\n'
docker compose ps vot
