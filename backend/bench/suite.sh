#!/usr/bin/env bash
# Full baseline-vs-optimised benchmark suite (sequential, ~1.5-2 h on 4 cores).
#
#   BASELINE=/path/to/original/backend OUT=/tmp/wsbench ./bench/suite.sh
#
# BASELINE must be the backend/ directory of a checkout of the original code (e.g. a git worktree of the
# commit before the scanner changes). Baseline runs execute FROM that directory so its `app` package is the
# one imported (python -m puts the working directory first on sys.path); every result records `app_code`.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
PY="$HERE/.venv/bin/python"
OUT="${OUT:-/tmp/wsbench}"
mkdir -p "$OUT"
: "${BASELINE:?set BASELINE to the backend directory of the original code}"

opt() { (cd "$HERE" && PYTHONPATH="$HERE" "$PY" -m bench.run --label optimised "$@" > /dev/null 2>"$OUT/last.err"); }
base() { (cd "$BASELINE" && PYTHONPATH="$HERE" "$PY" -m bench.run --label baseline "$@" > /dev/null 2>"$OUT/last.err"); }

for who in opt base; do
  $who --products 50 --scale 0.05 --out "$OUT/${who}50.json"
  for seed in 7 8 9; do
    $who --products 300 --seed $seed --scale 0.05 --repeat changed --quality --out "$OUT/${who}300_s$seed.json"
  done
  $who --products 1000 --scale 0.02 --repeat changed --quality --out "$OUT/${who}1000.json"
  $who --products 300 --scale 0.05 --server-args "--error-rate 0.05 --missing-rate 0.05 --malformed-rate 0.03 --slow-rate 0.02" \
    --out "$OUT/${who}300_faults.json"
done
WS_ANALYSIS_IMAGES=2 opt --products 1000 --scale 0.02 --quality --out "$OUT/opt1000_thorough.json"
opt --products 3000 --scale 0.01 --repeat unchanged --out "$OUT/opt3000.json"
base --products 3000 --scale 0.01 --out "$OUT/base3000.json"
echo "done: $OUT"
