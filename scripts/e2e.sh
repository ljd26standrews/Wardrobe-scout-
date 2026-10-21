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
.venv/bin/python scripts/demo_catalogue_server.py 18765 18766 > "$E2E_DIR/demo.log" 2>&1 & PIDS+=($!)
export WS_API_TOKEN="e2e-$(date +%s%N)"
# The synthetic catalogue runs on 127.0.0.1, so private URLs must be allowed for this test run only.
WS_ALLOW_PRIVATE_URLS=true WS_DATA_DIR="$E2E_DIR" WS_DATABASE_URL="sqlite:///$E2E_DIR/ws.db" WS_REQUEST_DELAY_SECONDS=0.05 WS_ENABLE_SCHEDULER=false \
  .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 18000 > "$E2E_DIR/api.log" 2>&1 & PIDS+=($!)
for i in $(seq 1 60); do curl -sf localhost:18000/api/health >/dev/null && break; sleep 0.5; done

api() { curl -sf -X "$1" "localhost:18000$2" -H 'Content-Type: application/json' -H "X-WS-Token: $WS_API_TOKEN" ${3:+-d "$3"}; }
api POST /api/sellers '{"name":"Demo Seller A","base_url":"http://127.0.0.1:18765","platform":"yupoo"}' >/dev/null
api POST /api/sellers '{"name":"Demo Seller B","base_url":"http://127.0.0.1:18766","platform":"yupoo"}' >/dev/null
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
