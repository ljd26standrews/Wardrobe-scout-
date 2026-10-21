#!/usr/bin/env bash
# Start backend (port 8000) and frontend (port 3000). Ctrl+C stops both.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [ -f "$ROOT/.env" ]; then set -a; . "$ROOT/.env"; set +a; fi
PIDS=()
cleanup() { for p in "${PIDS[@]:-}"; do pkill -P "$p" 2>/dev/null || true; kill "$p" 2>/dev/null || true; done; }
trap cleanup EXIT INT TERM

cd "$ROOT/backend"
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 & PIDS+=($!)
cd "$ROOT/frontend"
[ -d .next ] || node_modules/.bin/next build
node_modules/.bin/next start -p 3000 -H 0.0.0.0 & PIDS+=($!)

LAN_IP="$( (ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}') || true)"
sleep 3
echo
echo "  Wardrobe Scout is running:"
echo "    On this computer:  http://localhost:3000"
[ -n "$LAN_IP" ] && echo "    On your iPhone (same Wi-Fi):  http://$LAN_IP:3000"
echo "  Press Ctrl+C to stop."
wait
