# Selective recommendation: results, evidence and limits


This document records what was verified, how, and what is still unknown. One fact governs every number in it:
**no real approval labels exist in this environment.** Every performance figure below comes from synthetic
fixtures or simulated users. These are built to check that the machinery behaves correctly. They cannot show how
well the modes work for a real person. The real-label evaluator (`backend/eval/selective.py`) exists for that, and
it reports nothing until a real installation has enough rated pieces.

## 1. Verification performed

| Check | What it covers | Result |
|---|---|---|
| Backend test suite (`pytest`) | Labels, calibration, the gate, eligibility, feeds, logging, evaluator machinery, the trial, plus every earlier test | 259 passed at `67e5235` |
| End-to-end (`scripts/e2e.sh`, Playwright, desktop and iPhone) | Discover modes and tabs, empty states, Undo, Insights and Settings, against the synthetic demo catalogue | 16 passed at `67e5235` |
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

**Protocol.** The same 360-item synthetic catalogue as `eval/RESULTS.md`, with four personas:

- three that match, partly contradict, or oppose the written profile;
- `combo`, whose taste lives in colour × garment-type combinations.

Seeds 2–4, 15 rounds of 10 pieces each.

Each round shows the policy's main feed. When a gated feed shows fewer than 10 pieces, Learning picks fill the round, as Discover invites. The persona approves (love or like) pieces with utility ≥ 0.7, skips 0.5–0.7, and otherwise rejects with a reason. The runs include every fix in section 2. The raw rows are in `backend/eval/results/selective_sim_seeds2-4.jsonl`. The intervals pool the pieces, which are not independent within a run, so they are optimistic.

**Like for like: Balanced against the earlier ranked feed, in the same rounds of the same seed.**

| Persona | Rounds (of 45) | Balanced: approved / rated | Mean estimate | Earlier feed, same rounds |
|---|---|---|---|---|
| matches profile | not yet calibrated (36) | 95% [91%, 97%] | – | 100% [98%, 100%] |
| matches profile | calibrated (9) | 100% [95%, 100%] | 0.98 | 98% [91%, 100%] |
| partly differs | not yet calibrated (17) | 83% [74%, 90%] | – | 84% [76%, 90%] |
| partly differs | calibrated (28) | **77% [68%, 85%]** | 0.84 | **61% [52%, 69%]** |
| opposite | not yet calibrated (16) | 14% [8%, 21%] | – | 11% [6%, 18%] |
| opposite | calibrated (29) | nothing shown | – | 40% [32%, 47%] |
| combinations | not yet calibrated (25) | 11% [6%, 17%] | – | 13% [8%, 20%] |
| combinations | calibrated (20) | nothing shown | – | 10% [6%, 16%] |

- **Before calibration, Balanced behaves like the earlier feed.** That is expected: it is the same ranking with a similar share of exploration. The persona matching the profile approves slightly less (95% vs 100%), because its warm-up Learning picks bring the rejections it needs before it can calibrate. One of its three seeds never collected 5 rejections in 15 rounds, so it never calibrated.
- **Calibrated Balanced is more selective, and more precise when it does show something.** For the persona whose taste partly differs from the profile, it reached 77% against 61% for the earlier feed. Its mean estimate of 0.84 was optimistic by about 7 points, but within the interval. For the personas it cannot yet read, it abstains. The earlier feed delivered 40% and 10% to them, far below an 80% target.
- **Calibration took 6–14 rounds (60–140 decisions)** where it happened at all.

**The strict modes and two ablations of the gate (High confidence).**

| Persona | High: first round with a piece | High, no uncertainty (estimate ≥ 0.9) | High, no calibration (identity map) |
|---|---|---|---|
| matches profile | never | from round 6: 290 pieces, 100% [99%, 100%] | from round 8: 209 pieces, 100% [98%, 100%] |
| partly differs | round 15, in one seed (2 pieces) | from round 10: 26 pieces, 100% [86%, 100%] | never |
| opposite, combinations | never | never | never |

Extremely selective never showed a piece.

- **The conservative bound makes the strict modes very slow to show anything.** Within 150 decisions, High confidence showed 2 pieces in all. Gating on the estimate alone met the 90% target with plenty of coverage for the two learnable personas. So in this toy world the uncertainty term cost coverage without buying measurable precision.
- **That is not proof the bound is unnecessary.** These personas follow deterministic utility rules, and real tastes are noisier. The defaults are kept, because the brief puts precision first. Settings can lower a mode's confidence (High at 0.6 or 0.7, say), and Insights then shows whether the target still holds.
- **Learning picks found the opposite taste at 27% [20%, 34%]**, about what the earlier feed managed (28%), without ever entering the main feed.

**Earlier versions of these runs** found the defects listed in section 2: picks drying up, no exploration before calibration, and the plain-estimate floor. Those runs are not reported as results.

## 5. Interaction features (M7)

Colour × garment-type features have a tighter prior (variance 0.05, against 0.15 for single attributes), fixed beforehand. They were compared with the current engine on held-out seeds 2–9 (8 runs per cell, the `eval/RESULTS.md` protocol, precision@10 over 8 rounds):

| Persona | Current | With combinations | Paired difference [95% CI] | Wins |
|---|---|---|---|---|
| matches profile | 0.93 | 0.93 | 0.000 [−0.019, +0.019] | 2/8 |
| partly differs | 0.49 | 0.51 | +0.022 [−0.017, +0.067] | 5/8 |
| opposite | 0.15 | 0.17 | +0.019 [−0.005, +0.044] | 5/8 |
| combinations | 0.08 | 0.10 | +0.011 [−0.000, +0.023] | 5/8 |

**Decision: kept off.** The rule set before the run was "no persona harmed, and the combination persona demonstrably helped". Nobody was harmed, and three personas trended up. But the combination persona's gain is not demonstrated: its interval reaches zero. Eight rounds also give the shrunk combinations little data. A longer protocol would be a new experiment, and it was not run after seeing these results. Raw rows: `backend/eval/results/interactions_heldout_seeds2-9.jsonl`.

## 6. Computational cost (`bench/selective.py`)

Measured on this 4-vCPU cloud container with `bench/selective.py`:

- a synthetic catalogue, with a random photo embedding per piece and no photo files;
- 400 decisions, then one calibration;
- the median of 5 requests after a warm-up.

Absolute times depend on the machine; the comparisons are the point.

| Products | Balanced | High | Learning picks | Earlier ranked feed | Product page | Recalibration | Peak memory |
|---|---|---|---|---|---|---|---|
| 1,000 | 228 ms | 293 ms | 263 ms | 228 ms | 108 ms | 0.21 s | 217 MB |
| 5,000 | 1.18 s | 1.17 s | 1.08 s | 1.03 s | 136 ms | 0.64 s | 304 MB |
| 20,000 | 4.63 s | 5.01 s | 4.87 s | 4.57 s | 247 ms | 2.95 s | 605 MB |

- **The gate's own cost is small:** over the earlier feed it adds 0–29% at 1k (up to 65 ms), 5–15% at 5k and 1–10% at 20k. Its expensive part, each piece's uncertainty, is computed only where a bound could reach a target, or for the Learning-pick pool.
- **Most of the cost is in the ranking the gate builds on.** Making explanations lazy and loading candidates once cut every feed by a quarter to a third: Balanced went from 300 ms to 228 ms at 1k, 1.66 s to 1.18 s at 5k, and 7.0 s to 4.6 s at 20k (`bench/results/selective_before.jsonl`). This benchmark has no photo rows, so the photo-loading saving does not show here.
- **Recalibration** refits the model five times. It runs in the background after every 5 new decisions, so the feed never waits for it.
- **Still slow at 20,000 pieces.** Over 4 s per feed, after every decision, is too slow; section 9 lists it.

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

## 9. Remaining work

- **Real outcomes.**
  - Run `eval.selective` once a few hundred decisions and 40+ Evaluate grades exist.
  - Until then, every target and every simulation figure here is unvalidated for a real person.
- **The strict modes' conservatism.** Decide with real labels whether High and Extremely selective should default to a lower confidence. The synthetic evidence says they are very slow to show anything.
- **Feed latency at 20,000 pieces** (over 4 s).
  - The remaining cost is in loading and assessing every candidate on every request.
  - Caching candidates and quality per data version would remove most of it.
- **Calibration at the top.** A calibration fitted on pieces you were shown generalises imperfectly to the very highest estimates (section 4: hidden persona, 0.84 estimated vs 77% observed).
  - A map that emphasises the top of the range could help, or weighting the pieces the gate itself served.
  - Both need real labels to judge.
- **Interaction features** stay available behind a switch for a longer, pre-registered experiment.
