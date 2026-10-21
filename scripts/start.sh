#!/usr/bin/env bash
# Start backend (port 8000) and frontend (port 3000). Ctrl+C stops both.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [ -f "$ROOT/.env" ]; then set -a; . "$ROOT/.env"; set +a; fi
PIDS=()
cleanup() { for p in "${PIDS[@]:-}"; do pkill -P "$p" 2>/dev/null || true; kill "$p" 2>/dev/null || true; done; }
trap cleanup EXIT INT TERM

# The backend only ever listens on this computer; the app reaches it through its own proxy.
# The app itself is local-only unless you opt in to sharing it on your network with WS_LAN=1,
# which requires an access code (WS_ACCESS_CODE) so others on the same Wi-Fi can't use it.
APP_HOST=127.0.0.1
if [ "${WS_LAN:-0}" = "1" ]; then
  if [ -z "${WS_ACCESS_CODE:-}" ]; then
    echo "WS_LAN=1 needs an access code. Add WS_ACCESS_CODE=<something long> to .env and try again." >&2
    exit 1
  fi
  APP_HOST=0.0.0.0
fi

cd "$ROOT/backend"
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 & PIDS+=($!)
cd "$ROOT/frontend"
[ -d .next ] || node_modules/.bin/next build
node_modules/.bin/next start -p 3000 -H "$APP_HOST" & PIDS+=($!)

sleep 3
echo
echo "  Wardrobe Scout is running:"
echo "    On this computer:  http://localhost:3000"
if [ "$APP_HOST" = "0.0.0.0" ]; then
  LAN_IP="$( (ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}') || true)"
  [ -n "$LAN_IP" ] && echo "    On your iPhone (same Wi-Fi):  http://$LAN_IP:3000  (access code required)"
else
  echo "    (Local only. For your iPhone, set WS_LAN=1 and WS_ACCESS_CODE in .env.)"
fi
echo "  Press Ctrl+C to stop."
wait
