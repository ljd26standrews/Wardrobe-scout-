# Wardrobe Scout: correctness fixes and first redesign: handoff

This file is updated at each milestone so a later session (or a reviewer) can pick up the work.

## Where this starts

| | |
|---|---|
| Branch | `feature/redesign-and-fixes` (not merged, not deployed) |
| Base commit | `b37e41f51c3f6cb1f0f5fe7ebfe1c5d201bd58ca`, the head of `feature/selective-recommendation` (PR 1). It already contains `main` (57ba9e2), `audit/phase-1`, `audit/reliability-followup` and `perf/scanning`. |
| Data | Every run in this work used disposable databases and the synthetic demo catalogue (`backend/scripts/demo_catalogue_server.py`). No live database was opened. |

## Baseline, executed here on the base commit

| Check | Result |
|---|---|
| Backend tests (`pytest -q`, Python 3.13.16) | 259 passed in 165.6 s |
| `ruff check .` | All checks passed |
| `pip check` | No broken requirements |
| Frontend type-check (`tsc --noEmit`, Node 22.22.0) | exit 0 |
| `next lint` | 3 warnings, all pre-existing (unused `eslint-disable` directives in the Sellers and Settings pages) |
| `next build` | exit 0 |
| End-to-end (`scripts/e2e.sh`, Chromium from `/opt/pw-browsers`) | 16 of 16 passed |
| Screenshots | 36 captured with `frontend/scripts/screens.mjs` into `docs/redesign/screens/before/`: ten routes at 1440, 768 and 390 px wide in light mode, plus Discover, product and Saved in dark mode at 1440 and 390 px |

## Findings and their status

Status words:

- **historical**: reported by the earlier reviewer, not re-run here yet;
- **inspected**: seen in the code here;
- **reproduced**: demonstrated here by a failing test or run.

Each row's status is updated as the work lands.

| # | Finding | Status at the base |
|---|---|---|
| F1 | Calibration stays active after Undo takes the rated families from 30 to 29 (below the minimum). `needs_refit()` returns False whenever evidence is insufficient, so nothing withdraws it. | inspected (`calibration.needs_refit`) |
| F2 | Unsave (`DELETE /api/saved/{id}`) records an `unsave` event but never checks calibration. | inspected (`api/feedback.delete_saved`) |
| F3 | A calibration job activates its fit without rechecking that the labels it used are still current. A manual rollback (`activate`) is not validated at all. | inspected |
| F4 | Calibration is not tied to the model, visual representation, profile configuration or label-interpretation version. Only the engine version is in its hash. | inspected |
| F5 | The AUC in calibration diagnostics ranks ties by order rather than averaging them. | inspected |
| F6 | Evidence sources are deduplicated by the raw URL. Tracking parameters, fragments and host variants count as separate sources, and repeated favourable copies can outweigh one contrary excerpt from the same page. | inspected (`quality._source_key`) |
| F7 | The price median, value score, price ceiling and price filters compare raw numbers across currencies. | inspected (`quality.category_price_stats`, `recommender.build_profile`, `products._apply_filters`) |
| F8 | Labels conflate aesthetic preference with purchase suitability. Maybe counts as a negative, and Purchased counts as a positive on its own. | inspected (`approval_data.rated_outcomes`) |
| F9 | After two "already own" objections, the whole category is excluded, silently and with no way to reverse it. | inspected (`selective._strict_exclusion`) |
| F10 | Feed latency of 4.6 s at 20,000 products. | historical, to be re-measured |
| F11 | No CI, and backend dependencies are unpinned (`>=` ranges). | inspected |

## Plan and commit order

1. Baseline: this file, the screenshot script and the "before" screenshots.
2. Deterministic correctness: the calibration lifecycle (F1 to F4), AUC ties (F5), evidence identity (F6) and currency (F7).
3. Feedback and calibration contract: versioned label interpretation, "like the look, not now", Save semantics, and an ownership constraint in place of the category ban (F8, F9). Includes its additive migration.
4. Visual foundation and core screens: a design-system document, then the shell, Discover, product details and Saved, with consistent tokens elsewhere.
5. Performance and behaviour: revision-keyed caches, measurements, end-to-end timing cases, accessibility checks, after-screenshots and CI.

## How to reproduce the baseline

```bash
cd backend && .venv/bin/python -m pytest -q && .venv/bin/ruff check . && .venv/bin/pip check
cd ../frontend && npx tsc --noEmit && npx next lint && npx next build
cd .. && PW_EXECUTABLE_PATH=/opt/pw-browsers/chromium-1194/chrome-linux/chrome ./scripts/e2e.sh
# screenshots (serve the demo, then capture)
E2E_SERVE_ONLY=1 ./scripts/e2e.sh &
cd frontend && node scripts/screens.mjs http://localhost:13000 ../docs/redesign/screens/before
```
