# Selective recommendation: results, evidence and limits

> **Status: in progress.** Sections 4–6 (simulation, interaction features, computational cost) are still running
> and land in a later commit on this branch. Everything stated in the other sections has been run.

This document records what was verified, how, and what is still unknown. One fact governs every number in it:
**no real approval labels exist in this environment.** Every performance figure below comes from synthetic
fixtures or simulated users. These are built to check that the machinery behaves correctly. They cannot show how
well the modes work for a real person. The real-label evaluator (`backend/eval/selective.py`) exists for that, and
it reports nothing until a real installation has enough rated pieces.

## 1. Verification performed

| Check | What it covers | Result |
|---|---|---|
| Backend test suite (`pytest`) | Labels, calibration, the gate, eligibility, feeds, logging, evaluator machinery, the trial, plus every earlier test | 259 passed (commit `92baa5d` plus the paired-comparison tool) |
| End-to-end (`scripts/e2e.sh`, Playwright, desktop and iPhone) | Discover modes and tabs, empty states, Undo, Insights and Settings, against the synthetic demo catalogue | 16 passed at `80816a9`; to be re-run on the final commit |
| Migrations `e7c1a9d3b2f4`, `f3d2c8a1e6b9` | Up, down and up again on scratch databases with existing rows; `alembic check` reports no model drift | Passed; existing feed rows become policy `ranked` |
| Visual check | Discover, product page and Insights in the calibrated state (light, dark, mobile), on a throwaway instance seeded with the synthetic fixture | Done; four issues fixed (label collision, clipped axis, wrapped rows, a misleading bound sentence) |

Notable tests, each written to fail on the defect it guards against:

- **No label scores itself.** A rated piece's out-of-fold score does not move when its own label flips. This holds also when the background weight is small enough not to be capped: the test for that case failed before the leak fix, −1.179 vs −1.308.
- **The evaluator never sees the future.** Changing a label in the last block leaves every earlier prediction unchanged.
- **Thresholds never relax.** A stricter target never admits more pieces, and an empty mode explains itself and is not padded.
- **No percentage without a calibration.** This also holds when the calibration store fails: the feed degrades to `unavailable` and shows nothing as high-confidence.
- **Learning picks.** Logged inclusion probabilities match empirical inclusion frequencies (4,000 draws, within 4 standard errors). Rated picks are replaced by fresh draws.

## 2. Defects found by the evaluation work, and fixed

| Found by | Defect | Fix |
|---|---|---|
| Audit (M1) | A "logo" rejection also lowered the brand | Logo reasons teach logo visibility only |
| Audit (M1) | Insights' top picks bypassed any gate | They go through the same gate as Discover |
| Writing the evaluator (M6) | Out-of-fold calibration leaked: the background sample's weight counted held-out approvals | Held-out families are excluded before the background is built |
| Selective simulation | Learning picks dried up within a day. The draws were fixed per day, so once the day's picks were rated almost none followed, and the strict modes were starved of labels | Fresh draws after every decision; probabilities stay exact |
| Selective simulation | Before calibration, Balanced had no exploration at all, so a taste far from the written profile was found slowly | A few labelled Learning picks, in uncalibrated Balanced only |
| Selective simulation | Learning picks were limited to pieces with a plain estimate of at least 0.35, so a contrarian taste was never explored | Optimistic plausibility (mean + 1 s.d.) and a pool that widens instead of stalling |
| Selective simulation | Calibrated Balanced could be optimistic about its top pieces | Discover now warns when observed approval is demonstrably below the target |

## 3. Synthetic evaluation of the machinery (`eval/selective.py`)

A clearly synthetic database was used (`tests/test_eval_selective.timed_world`). Approval probabilities are known by colour and garment type (0.04–0.97), 288 decisions arrive in random order over time, and 60 pieces carry Evaluate-style grades. The evaluator ran exactly as it would on real data, with blocks of 20.

**Rolling origin (288 decisions; 258 predicted with a calibrated estimate):**

| Estimate | Brier | ECE (noise floor) | AUC |
|---|---|---|---|
| raw σ(m) | 0.132 | 13% (~6%) | 0.91 |
| calibrated | 0.123 | 12% (~6%) | 0.91 |

| Mode | Coverage | Gate precision [95% CI] | Estimate only (no uncertainty) |
|---|---|---|---|
| Balanced (≥ 80%) | 27% | 96% [88%, 99%] of 70 | same |
| High (90% at 80% confidence) | **0%** | nothing selected | 100% [86%, 100%] of 24 |
| Extremely selective (95% at 90%) | **0%** | nothing selected | nothing selected |

Showing every rated piece would have given 58% [51%, 64%] (the base rate).

**Reading.**

- **Balanced.** Its selectivity pays here: 96% against a base rate of 58%. At γ = 0.5, the gate and the plain taste ranking pick the same pieces at the same coverage by construction. Balanced's job is deciding *how many* pieces to show, not *which*.
- **The strict modes never fire at this amount of evidence.** Each piece's logit uncertainty is about 0.6. It is dominated by the 16 image components, which are pure noise in this fixture, plus about 0.37 from the calibration's own uncertainty. That holds the best piece's 80% bound near 0.886, just under 0.90. Gating on the estimate alone would have met the High target on 24 pieces. So the conservative bound looks stricter than the target needs here, at the cost of coverage. That is a trade-off to revisit with real labels, not a demonstrated error.
- **Calibration error.** It stays about twice its noise floor, so the estimates are only roughly calibrated in this fixture.

**Evaluate grades (60 graded pieces).** Balanced selected 47% of graded pieces at 86% [67%, 96%] precision. High and Extremely selective selected none. The sample is too small for the threshold-suggestion step to say much.

## 4. Simulated users and the modes (`eval/simulate_selective.py`)

_Pending: the run in progress uses the fixes listed in section 2. Results, per persona and policy, split by calibration state, with the ablations (no uncertainty in the gate; no calibration), will be added here._

## 5. Interaction features (M7)

_Pending: colour × garment-type features are implemented behind a switch (off). They will be compared with the current engine on held-out seeds 2–9 for every persona, including one whose taste is in combinations, and kept off unless they help without harming anyone._

## 6. Computational cost (`bench/selective.py`)

_Pending: latency of each feed and peak memory at 1k, 5k and 20k products, to be measured on an otherwise idle machine._

## 7. Reliability and calibration limits

- **No real-data evidence.** Every number here is synthetic. Whether any mode meets its target for a real person is unknown until that person's own outcomes accumulate. Insights shows that, with intervals.
- **The logged decisions are not a random sample.** They are the pieces you were shown and chose to rate. The main feed is deterministic and logs no propensities, so no inverse-propensity estimate is claimed for it. Only Learning picks carry exact inclusion probabilities. Evaluate grades are the unbiased sample.
- **Calibration has a narrow base.** It is a two-parameter map fitted on a few dozen to a few hundred labels. It assumes a monotone, roughly logistic relation and is weakest at the extremes, which is exactly where the strict modes operate.
- **The uncertainty model has its own assumptions.** It is a Laplace approximation of a logistic model. The bound treats the item's logit and the calibration parameters as Gaussian. It does not account for model misspecification, for example taste that depends on combinations of attributes.
- **Selection effects.** Picking the highest estimates from hundreds of candidates favours pieces whose estimates are too high. Discover's shortfall warning is the safeguard. Nothing adjusts silently.
- **Skips are not labels.** The estimate is the probability of approval *given that you rate the piece*. A feed you mostly skip says little.

## 8. Running it on real data

```bash
cd backend && .venv/bin/python -m eval.selective          # on a read-only copy of your database
.venv/bin/python -m eval.real --freeze                     # once, before tuning: freeze your Evaluate grades as a test set
```

- **When it reports.** The rolling-origin protocol starts predicting after 10 rated pieces, gives calibrated figures once 30 rated pieces (with 5 approvals and 5 rejections) precede an origin, and refuses any calibration metric below 50 predictions and any precision below 10 selected pieces. The Evaluate-grade protocol needs 40 grades, including 5 liked.
- **Output.** Results (aggregates only) are saved under `data/evaluation/`.
- **What would show it works.** For each mode, the lower end of the precision interval clearing its target, on both protocols, with coverage high enough to be useful.
