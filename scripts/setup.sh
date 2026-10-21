#!/usr/bin/env bash
# One-time setup on macOS / Linux. Requires Python 3.11+ and Node.js 20+.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/backend"
PY="${PYTHON:-python3}"
"$PY" -c 'import sys; assert sys.version_info >= (3, 11), "Python 3.11+ required"'
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/pip install --upgrade pip >/dev/null
.venv/bin/pip install -r requirements-dev.txt
if [ "${WITH_CLIP:-0}" = "1" ]; then .venv/bin/pip install -r requirements-ml.txt; fi
if [ "${WITH_BROWSER:-0}" = "1" ]; then .venv/bin/playwright install chromium; fi
.venv/bin/python -m app.cli init-db
cd "$ROOT/frontend"
npm install --no-audit --no-fund
npm run build
[ -f "$ROOT/.env" ] || cp "$ROOT/.env.example" "$ROOT/.env"
echo
echo "Setup complete. Start the app with: ./scripts/start.sh"
