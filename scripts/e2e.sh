#!/usr/bin/env bash
# End-to-end test run against a THROWAWAY database seeded from the synthetic demo catalogue.
# Never touches your real data directory.
#   ./scripts/e2e.sh            # build frontend, start everything, run Playwright, stop
# Env: PW_EXECUTABLE_PATH (optional Chromium path), E2E_SCREENSHOT_DIR (optional)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
E2E_DIR="${E2E_DIR:-$ROOT/.e2e-data}"
rm -rf "$E2E_DIR" && mkdir -p "$E2E_DIR"
PIDS=()
cleanup() { for p in "${PIDS[@]:-}"; do pkill -P "$p" 2>/dev/null || true; kill "$p" 2>/dev/null || true; done; }
trap cleanup EXIT

cd "$ROOT/backend"
.venv/bin/python scripts/demo_catalogue_server.py 18765 18766 18767 > "$E2E_DIR/demo.log" 2>&1 & PIDS+=($!)
export WS_API_TOKEN="e2e-$(date +%s%N)"
# The synthetic catalogue runs on 127.0.0.1, so private URLs must be allowed for this test run only.
WS_ALLOW_PRIVATE_URLS=true WS_DATA_DIR="$E2E_DIR" WS_DATABASE_URL="sqlite:///$E2E_DIR/ws.db" WS_REQUEST_DELAY_SECONDS=0.05 WS_ENABLE_SCHEDULER=false \
  .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 18000 > "$E2E_DIR/api.log" 2>&1 & PIDS+=($!)
for i in $(seq 1 60); do curl -sf localhost:18000/api/health >/dev/null && break; sleep 0.5; done

api() { curl -sf -X "$1" "localhost:18000$2" -H 'Content-Type: application/json' -H "X-WS-Token: $WS_API_TOKEN" ${3:+-d "$3"}; }
api POST /api/sellers '{"name":"Demo Seller A","base_url":"http://127.0.0.1:18765","platform":"yupoo"}' >/dev/null
api POST /api/sellers '{"name":"Demo Seller B","base_url":"http://127.0.0.1:18766","platform":"yupoo"}' >/dev/null
# Awkward listings for layout and honesty checks (long title, no price, landscape and broken photos, many offers)
api POST /api/sellers '{"name":"Demo Seller C","base_url":"http://127.0.0.1:18767","platform":"yupoo"}' >/dev/null
# Pieces priced in currencies with no rate set (only yuan has one by default): shown as listed, never converted
manual() { curl -sf -X POST localhost:18000/api/import/manual -H "X-WS-Token: $WS_API_TOKEN" -F "seller_name=Demo Imports" "$@" >/dev/null; }
manual -F "title=[DEMO] Wool scarf priced in won" -F "price=89000" -F "currency=KRW" -F "category=accessories"
manual -F "title=[DEMO] Linen shirt priced in US dollars" -F "price=95" -F "currency=USD" -F "category=shirt"
# Nothing leaves this computer: the starting sellers (real shops) are paused and their watched links removed, so
# the run never loads, or depends on, a real website. A seller on a port where nothing listens is a genuine failure.
api POST /api/sellers '{"name":"Demo Seller D","base_url":"http://127.0.0.1:18768","platform":"yupoo"}' >/dev/null
for id in $(api GET /api/sellers | python3 -c 'import sys,json;print(" ".join(str(s["id"]) for s in json.load(sys.stdin) if not (s.get("base_url") or "").startswith("http://127.0.0.1")))'); do
  api PATCH "/api/sellers/$id" '{"status":"paused"}' >/dev/null
done
for id in $(api GET /api/watched | python3 -c 'import sys,json;print(" ".join(str(w["id"]) for w in json.load(sys.stdin)))'); do
  api DELETE "/api/watched/$id" >/dev/null
done
JOB=$(api POST /api/sellers/scan-all '{"mode":"full"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["job_id"])')
for i in $(seq 1 120); do
  ST=$(api GET "/api/jobs/$JOB" | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"])')
  [[ "$ST" == queued || "$ST" == running ]] || break; sleep 1
done
echo "demo scan: $ST"

cd "$ROOT/frontend"
[ -d .next ] || node_modules/.bin/next build
BACKEND_URL=http://127.0.0.1:18000 node_modules/.bin/next start -p 13000 > "$E2E_DIR/web.log" 2>&1 & PIDS+=($!)
for i in $(seq 1 60); do curl -sf localhost:13000 >/dev/null && break; sleep 0.5; done
if [ -n "${E2E_SERVE_ONLY:-}" ]; then echo "serving on http://localhost:13000"; wait; fi
E2E_BASE_URL=http://localhost:13000 npx playwright test "$@"
