#!/usr/bin/env bash
# Development mode with auto-reload for both servers.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [ -f "$ROOT/.env" ]; then set -a; . "$ROOT/.env"; set +a; fi
trap 'kill 0' EXIT INT TERM
(cd "$ROOT/backend" && .venv/bin/uvicorn app.main:app --reload --host 127.0.0.1 --port 8000) &
(cd "$ROOT/frontend" && node_modules/.bin/next dev -p 3000 -H 127.0.0.1) &
wait
