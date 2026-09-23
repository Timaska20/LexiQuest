#!/usr/bin/env bash
set -u
cd "$(dirname "$0")"

echo "=== LexiQuest Cake ==="
[ -f VERSION ] && printf 'version: ' && cat VERSION

echo
echo "=== Containers ==="
docker compose ps -a || exit 1

echo
echo "=== VOT ==="
docker compose exec -T vot cat /app/vot-version.txt 2>/dev/null || true
docker compose logs --tail=20 vot 2>/dev/null || true

echo
echo "=== Recent errors ==="
docker compose logs --since=30m 2>/dev/null | grep -iE 'error|exception|failed|traceback|UND_ERR' | tail -50 || echo "No recent matching errors"
