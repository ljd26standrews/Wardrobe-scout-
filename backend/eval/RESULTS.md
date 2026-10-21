# Recommender evaluation results

These are offline simulation results from `eval/simulate.py`, with raw rows in `eval/results/`. They measure
how quickly each engine learns a simulated person's taste. They are **not** real-world accuracy:
the catalogue is synthetic, the images are simple drawings, and taste is a function of five attributes.

## Protocol

- 360 synthetic items: category, colour, material, fit and logo are written into the titles, so the real parser extracts them, and drawn into the images, so the real image embedding sees them.
- Each round, the persona sees the top 10 of the Discover feed. It loves, likes, skips or dislikes each item, and gives the rejection reason a person would give. There are 8 rounds, for 80 items shown in total.
- The metric is **precision@10**: the share of shown items the persona actually wants. Wanted items make up 9–40% of the catalogue, depending on the persona.
- The three personas:

  | Persona | Taste | Wanted share of catalogue |
  |---|---|---|
  | `stated` | Matches the written profile. | 40% |
  | `hidden` | Earth tones, cardigans and coats; dislikes hoodies and tees, contradicting part of the profile. | 18% |
  | `contrarian` | Summer clothes (linen, light colours, shirts and shorts), the opposite of the profile. | 9% |

- The four methods:

  | Method | What it is |
  |---|---|
  | `random` | Random ranking. |
  | `rules_only` | The written profile only, with no learning. |
  | `legacy` | The pre-audit hand-weighted engine. |
  | `bayes` | The new default engine. |

- **Calibration vs. held out.** Hyper-parameters (prior variances, prior scale) were chosen on **seed 1 only**. Seeds 2–9 were not used for any tuning, and all the numbers below come from them.

## Tranche 3 model fixes (October 2026): no measurable change, as intended

These are correctness fixes, each covered by a deterministic test in `tests/test_taste_model.py`:

- **R1.** Image components exist from 3 photographed products (previously 18 were needed).
- **R2.** A colour-only rejection moves only colour: the background sample now needs something you chose.
- **R3.** "This specific item" teaches no general style.
- **Families.** One garment listed by several sellers counts once, and its listings are never background.

They were run side by side on the held-out seeds 2–9 (8 runs per cell) against the **unchanged engine**: commit
`43977d2`, run from a separate checkout. Two ablations of the background sample ran with them.

| Persona | Unchanged | Fixed | No background sample | Background at full weight from the start |
|---|---|---|---|---|
| stated | 0.93 | 0.94 | 0.93 | 0.94 |
| hidden | 0.47 | 0.48 | 0.49 | 0.48 |
| contrarian | 0.16 | 0.16 | 0.15 | 0.16 |

The table shows mean precision@10 over 8 rounds. Paired differences per seed, with 95% bootstrap intervals over
the 8 seeds:

| Persona | Fixed − unchanged | No background − fixed |
|---|---|---|
| stated | +0.005 [−0.006, +0.014] | −0.006 [−0.020, +0.008] |
| hidden | +0.002 [−0.025, +0.028] | +0.013 [−0.011, +0.039] |
| contrarian | +0.005 [−0.025, +0.034] | −0.011 [−0.037, +0.016] |

- **The fixes cost no learning performance here.** They also show no gain: this simulation never exercises what
  they fix. It has no "specific item" reasons, no re-listed garments, and 360 photographed products. Its seed
  examples are positives from the start, so a rejection never arrives with nothing chosen.
- **The background sample is not demonstrated either way.** Removing it is within noise. It stays, at its
  current scaled weight, until real labels can test it. "Full weight from the start" gave results identical to
  "fixed" in every run.

Raw rows: `results/tranche3_heldout_seeds2-9.jsonl`.

## Held-out results (seeds 2–9, 8 runs per cell)

| Persona | random | rules_only | legacy | **bayes** |
|---|---|---|---|---|
| stated | 0.37 | 0.82 | 0.68 | **0.93** |
| hidden | 0.18 | 0.39 | 0.38 | **0.48** |
| contrarian | 0.11 | 0.05 | 0.17 | 0.16 |

The table shows mean precision@10 over all 8 rounds.

### Paired difference per seed

The 95% intervals are from a bootstrap over the 8 seeds.

| Persona | bayes − rules_only | bayes − legacy |
|---|---|---|
| stated | +0.11 [+0.09, +0.12], wins 8/8 | +0.25 [+0.22, +0.29], wins 8/8 |
| hidden | +0.09 [+0.04, +0.14], wins 6/8 | +0.11 [+0.08, +0.14], wins 8/8 |
| contrarian | +0.11 [+0.08, +0.14], wins 8/8 | −0.01 [−0.04, +0.02], wins 3/8 |

### By phase (precision@10)

| Persona | Method | Round 1 | Rounds 2–4 | Rounds 5–8 |
|---|---|---|---|---|
| stated | legacy | 0.90 | 0.72 | 0.58 |
| stated | bayes | 0.93 | 0.96 | 0.90 |
| hidden | legacy | 0.28 | 0.47 | 0.33 |
| hidden | bayes | 0.17 | 0.56 | 0.50 |
| contrarian | legacy | 0.00 | 0.15 | 0.24 |
| contrarian | bayes | 0.01 | 0.10 | 0.25 |

## Interpretation

- **The legacy engine got worse the more it was used.** For the `stated` persona it fell from 0.90 to 0.58 as it went on, because its novelty and diversity terms pushed out items that matched the profile. The Bayesian engine holds 0.90 or better.
- **Learning is real.** For `hidden` and `contrarian`, Bayes beats the static profile (`rules_only`) by about 0.1. The profile's trust weight is learned, so behaviour can override what was written.
- **Weakness: contrarian users.** When someone's real taste is the opposite of their written profile, Bayes is no better than legacy. It is slower to let go of the profile in rounds 2–4 (0.10 vs 0.15) and only catches up later. In practice the fix is to edit the written profile; the Insights page shows the learned trust in it.
- **Weakness: round 1 for `hidden`.** Round 1 is worse for `hidden` (0.17) because about 2 of 10 slots are Thompson exploration picks. That cost is deliberate, and rounds 2–8 recover it.

## Calibration (seed 1)

| Config (attribute prior var, profile scale) | stated | hidden | contrarian |
|---|---|---|---|
| A (0.15, 4) | 0.85 | 0.45 | 0.31 |
| B (0.30, 4) | 0.89 | 0.47 | 0.33 |
| **C (0.15, 2) — chosen** | 0.89 | 0.45 | 0.33 |
| D (0.30, 2) | 0.85 | 0.46 | 0.28 |

The differences between configurations are within seed-to-seed noise. C was kept because it ties for best on
two of the three personas and has the weaker profile prior, which helps users whose taste departs from what they wrote.

## Reproduce

```bash
cd backend
for p in stated hidden contrarian; do
  .venv/bin/python -m eval.simulate $p random,rules_only,legacy,bayes 2,3,4,5,6,7,8,9 > heldout_$p.log
done
.venv/bin/python -m eval.report heldout_*.log
```

A full run takes about 4 minutes per persona on 4 cores. `tests/test_eval_regression.py` re-runs a short
version on every `pytest`.
