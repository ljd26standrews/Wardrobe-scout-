# Selective recommendation: design and plan

## 1. Labels and target

- **Target quantity:** `P(Y = 1 | you, item, features, item shown and explicitly rated)`.
- **Y = 1:** love, like (save) or purchased.
- **Y = 0:** dislike, for any reason, and "maybe". A maybe is not an approval, so the metric is conservative.
- **No label:**
  - skips, and items displayed but not rated;
  - items never shown. The taste model's "typical items" background sample is a modelling device, never counted as a rejection in any metric.
  - decisions you undid. Undo deletes the row, so none remain.
- **Families:** one label per garment family, taken from the latest decision on any of its listings. Photos of one garment are never independent evidence.
- **Representative labels:** the Evaluate page's uniform, blinded taste grades, where grade ≥ 2 counts as approval. These are used for evaluation, never for training.

## 2. Approaches considered

| Approach | Strengths | Weaknesses | Data needed | Cost |
|---|---|---|---|---|
| Platt or isotonic on the current feed score | Trivial | The feed score mixes taste, quality, value and seller on different scales; isotonic overfits small samples | Isotonic: hundreds of labels | Negligible |
| **Bayesian posterior predictive + cross-fitted Bayesian Platt on the taste logit** | Reuses the current model; per-item uncertainty from the posterior; calibration uncertainty from a 2-parameter posterior; honest when data is scarce | Laplace is an approximation; Platt assumes a monotone, roughly logistic miscalibration | ~30 rated garments to start; improves continuously | One d×d inverse per data change; K refits per recalibration |
| Separate approval model on explicit outcomes only (many features) | Models price and redundancy objections directly | Too many parameters for the first hundred labels; unstable | Hundreds of labels | Low |
| Bootstrap ensemble of the logistic model | Captures some misspecification | B refits per change; variance is unstable with small samples | Same as the chosen approach | B× fit cost |
| Gradient-boosted trees | Interactions for free | One user rarely reaches the thousands of labels needed; poorly calibrated without a large holdout | Thousands | Moderate |
| Conformal or guaranteed-risk thresholds | Distribution-free risk bound | Needs exchangeable calibration data, which the exposure-biased Discover logs are not (the uniform Evaluate grades are) | A held-out split | Negligible |

**Chosen:** the Bayesian posterior predictive with cross-fitted Bayesian Platt calibration, gated per item on a conservative bound. Guaranteed-risk thresholds are used in **evaluation**, on the uniform grades, to check and suggest thresholds. Nothing heavier is justified until real labels show the simple model is inadequate.

## 3. Architecture (stages A–F)

### A. Candidates

Unchanged: `feed_candidates` and `collapse_families`.

### B. Eligibility

Every decision is recorded with its reason.

- **All modes:**
  - existing rules (active, undecided, family, skip window, not seed);
  - near-duplicates of rejected items are withheld;
  - garments with **known** poor quality (independent evidence scoring below 40) are withheld. Unknown quality stays unknown and is never treated as poor.
- **High and Extremely selective additionally require:**
  - an analysed garment photo;
  - not a seller with recorded concerns;
  - not above your learned "too expensive" ceiling for the category, unless quality evidence supports the price;
  - not a category you have repeatedly said you already own.
- **Optional:** require independent quality evidence.

### C. Compatibility

Existing Bayesian logistic taste model, with three changes:

- full-covariance Laplace posterior;
- a logo rejection teaches about logos only, not the brand;
- three new optional reasons: "not my style" (taste), "wrong category" (data error, teaches nothing) and "too repetitive" (no taste learning; feeds diversity).

The model produces `m_i` (mean logit = raw compatibility) and `s_i` (posterior sd of the logit).

### D. Calibration

1. Out-of-fold logits for every rated garment family: K = 5 folds grouped by family; each fold's taste model is fitted without that fold's decisions.
2. Bayesian Platt fit on the out-of-fold logits: `u = a·m + b`, with priors `a ~ N(1, 0.5²)`, `b ~ N(0, 1²)`, giving a Laplace posterior over `(a, b)`.
3. Estimates for each item:

   | Name | Formula | Meaning |
   |---|---|---|
   | Approval estimate | `p̂_i = σ(â·m_i + b̂)` | Calibrated estimate of approval |
   | Conservative bound | `p_low_i(γ) = σ(â·m_i + b̂ − z_γ·σ_u)` | Lower bound at confidence γ |
   | Bound spread | `σ_u² = â²·s_i² + [m_i 1]·S_ab·[m_i 1]ᵀ` | Combines the model's uncertainty about this item with the calibration's own uncertainty |

**Sufficiency:** calibration is used only with at least 30 rated garment families, including at least 5 approvals and 5 rejections. All three numbers are configurable. Below that the status is *insufficient*, and no probability is shown anywhere.

### E. Gate

An item passes mode (t, γ) iff `p_low_i(γ) ≥ t`. Under the model, that means you are at least γ sure its approval probability is at least t.

| Mode | t | γ | Below sufficiency (uncalibrated) |
|---|---|---|---|
| Balanced | 0.80 | 0.50 (the estimate itself) | The existing taste ranking, labelled "not yet calibrated"; no exploration mixed in (it lives in Learning picks) |
| High confidence | 0.90 | 0.80 | Empty, with what is needed |
| Extremely selective | 0.95 | 0.90 | Empty, with what is needed |

- **Targets are aspirations.** The 80/90/95% figures are targets for later validation, not achieved precision.
- **Thresholds never drop.** The gate is never relaxed to fill the feed.
- **Empty feeds explain themselves:** what qualified under other modes, and a link to Learning picks.

### F. Ranking

- **Order:** gated items by `p_low`.
- **Diversity:** a greedy pass avoids more than two consecutive items from the same category or seller. It reorders only; membership never changes.
- **Strict modes:** High and Extremely selective have no exploration slots.

### Learning picks (separate, optional, labelled)

- **Optimistic plausibility:** `q_i = σ(â·(m_i + s_i) + b̂)`, the estimate one s.d. above the mean logit. A piece the model knows little about can be tried even when its plain estimate is low; a piece it is sure you would reject cannot. (A first version used `p̂ ≥ 0.35`. In the selective simulation, the persona whose taste is the opposite of the written profile then never got picks from the region it actually likes.)
- **Pool:** eligible items that did not pass the current mode, with `q ≥ 0.35`. When fewer than three times the picks wanted qualify, the pool widens to the next most plausible pieces instead of stalling learning.
- **Weights:** `w_i = q_i² · s_i`, boosted for categories with few labels.
- **Before calibration, in Balanced only:** a few labelled Learning picks (the exploration rate, 18% by default) are interleaved into the feed, which is then labelled as a plain taste ranking. They never appear in a calibrated feed or in the strict modes.
- **Sampling:** Poisson sampling with inclusion probability `π_i = min(1, K·w_i/Σw)`. The probability is logged, so impressions from this feed support inverse-propensity estimates.
- **Main feed:** deterministic. The log records no propensity for it, so inverse-propensity estimates cannot be made for it, and none are claimed.
- **Stable between decisions:** each piece's inclusion draw uses a random number fixed by a hash of (day, mode, number of decisions so far, piece). Refreshing does not reshuffle the picks; every decision brings fresh draws, and an Undo returns to the earlier ones. Each request's inclusion probability is exactly the logged π. (A first version fixed the draws for the whole day: once the day's picks were rated, almost no new ones appeared. The selective simulation caught it.)
- **Without a calibration:** the pool is chosen on the raw model's σ(m). It is used only to choose, and never shown as a probability.

### Updating

- **Taste model:** refitted on every data change, with the posterior cached by data version.
- **Calibration:** a background job, queued when at least 5 new rated families have arrived since the last fit (or none exists). Each fit is stored with its diagnostics.
- **Rollback:** reactivate a previous calibration, or switch the gate off with the feature flag.
- **Feed log:** every served feed records the policy, mode, calibration id, and each item's `m`, `p̂`, `p_low` and gate decision.

## 4. Evaluation

- **Script:** `eval/selective.py`, on real labels only.
- **Logged protocol:** Discover decisions, in time order and family-disjoint. Rolling origin: fit and calibrate on earlier labels, predict the next block.
  - Calibration: ECE, Brier score and reliability bins, each with counts.
  - Precision–coverage curves for gating on raw, calibrated and conservative estimates.
  - Per-mode precision with Clopper–Pearson intervals.
  - Learning progression by training size.
- **Representative protocol:** the uniform Evaluate grades as an untouched test set.
  - Same metrics, plus an unbiased coverage estimate.
  - Baseline: the current ranking's precision@K.
  - Thresholds are suggested on a validation half and reported on the test half.
- **Ablations:** calibration; uncertainty in the gate; family deduplication; reason masks; quality eligibility; personalised versus profile-only; interaction features.
- **Refusals:** no metric is reported below its minimum sample size.
- **Synthetic checks:** synthetic fixtures verify correctness only, for example that calibration recovers a known logistic and that intervals cover. The simulation shows gate behaviour and is labelled synthetic.
- **Prospective trial (opt-in):** a daily switchback between the old and selective policies. The policy is logged; approval is compared with intervals.

## 5. Milestones (each a reviewable commit, tests first)

1. Labels and rated-outcome extraction (undo-, family- and maybe-aware); logo reason repair; new optional reasons.
2. Full-covariance posterior, posterior cache, predictive `m`/`s`.
3. Calibration service, `calibrations` table (additive migration), recalibration job, sufficiency, rollback.
4. Gate service, `selective` settings, Stage B eligibility, ranking; feed, learning, insights and status endpoints; feed-log columns (additive migration).
5. Frontend: mode selector, status, Learning picks tab, honest confidence display, empty states, gate-consistent Undo, observed performance.
6. `eval/selective.py`, synthetic correctness fixtures, simulation of the modes, ablations.
7. Interaction features (shrunk), kept only if the simulation shows no harm and an interaction persona benefits.
8. Latency and memory benchmarks at 1k, 5k and 20k products; prospective-trial flag; documentation.

## 6. As built (milestone 4)

- **States:**

  | State | When | Main feed |
  |---|---|---|
  | `calibrated` | An active calibration exists | Pieces passing the mode, ordered by bound |
  | `uncalibrated` | Too few labels | Balanced: the ranking, labelled. Strict modes: empty, saying how many labels are needed |
  | `pending` | Enough labels; the calibration job has not finished | As `uncalibrated` |
  | `unavailable` | The model or calibration raised, or the legacy engine is selected | As `uncalibrated`, saying why |
  | `off` | `selective.enabled` is false | The earlier feed, with exploration slots (rollback) |

- **Eligibility constants:**
  - known poor quality means an independent-evidence quality score below 40;
  - a price at or above 0.9× your "too expensive" ceiling for the category is withheld unless quality evidence scores at least 60;
  - "already own" given twice for a category withholds that category.

  Every exclusion is counted with its reason in the response.
- **One gate everywhere.** Discover, Learning picks, the Insights top picks and the product page all call the same `selective.evaluate`. A piece's confidence block always comes from the server.
- **Logged per served item:** `m`, `s`, `estimate`, `bound`, the modes passed and, for Learning picks, `propensity`.
- **Logged per feed:** `policy`, `mode`, `calibration_id`, and `gate` (state, thresholds, counts, sampling details). This uses additive migration `f3d2c8a1e6b9`.
- **Observed performance:** `/api/selective/status` reports approval among served pieces that were then rated, by serving mode and by gate passed, with 95% Clopper–Pearson intervals. Nothing is reported below 10 rated pieces. Pieces shown but not rated are not counted.
