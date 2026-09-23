#!/usr/bin/env bash
set -euo pipefail
if [ ! -f .env ]; then echo ".env not found"; exit 1; fi
grep -E '^(LEXIQUEST_PORT|APP_USER|APP_PASSWORD)=' .env
