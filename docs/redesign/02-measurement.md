# Measurement: what was fixed, what was audited, and how a fair real-data test should run

Everything below was executed in this branch on synthetic fixtures, unless it says otherwise. None of it is evidence
about real taste accuracy, which remains unproven until the real-data plan at the end has been carried out.

## Fixed

### AUC with tied scores (reproduced, fixed, verified)

On the base commit, the calibration diagnostics ranked tied scores by the order they happened to be listed in.
`calibration._auc` compared with `sklearn.metrics.roc_auc_score` on the same inputs:

| Input | Base commit | Reference | After the fix |
|---|---|---|---|
| Ties across the two outcomes | 0.556 | 0.778 | 0.778 |
| Constant scores (no separation) | 0.0 | 0.5 | 0.5 |
| Perfect ranking | 1.0 | 1.0 | 1.0 |
| Reversed ranking | 0.0 | 0.0 | 0.0 |
| Approvals listed first within ties | 0.25 | 0.5 | 0.5 |
| Only one outcome present | None | undefined (warning, NaN) | None |

It now uses the Mann-Whitney statistic with average ranks, so a tie counts one half. `tests/test_metrics.py`
checks it against scikit-learn on these cases and on 50 heavily tied random draws.

### Reported calibration error no longer flatters the chosen map (inspected, fixed)

The app picks between the identity map and Platt scaling by their out-of-fold Brier scores. It then reported the
winner's out-of-fold score. Choosing on the same scores you report is optimistic.

The reported figures are now nested: for each fold, the choice is remade on the other folds alone and applied to
that fold. Settings, Insights and the reliability diagram show these figures. Both raw and Platt figures are still
stored. A hand-computed check is in `tests/test_metrics.py`.

### Ties at a coverage cut (inspected, fixed)

Comparisons at the same coverage used to break tied scores by list order. This applied to:

- precision at coverage;
- the taste-ranking baseline at the gate's coverage;
- the Discover-score baseline;
- NDCG, precision@10 and liked@20 in `eval/real.py`.

Each now uses its exact expectation over random tie orders, so no method gains from an arbitrary tie order. The
tests compare these against brute force over every order of the tied pieces. A constant score is now exactly the
random baseline, and the random baseline is exact rather than sampled.

## Leakage audit of the second stage (calibration)

| Path | Finding |
|---|---|
| Out-of-fold taste scores (`calibration.out_of_fold`) | Families are held out of training, of the background sample and of its weight. Existing tests cover both. No leak. |
| Platt step in diagnostics | Refitted without each fold. No leak. |
| Choice of identity or Platt | **Selection bias in the reported error.** Fixed above (nested). |
| Rolling-origin evaluation (`eval/selective.py`) | Taste model and the whole calibration procedure are fitted with `before=cutoff` and the block's families excluded. The identity-or-Platt choice uses training labels only. No leak. |
| Representative protocol | Decisions about graded families are deleted from the copy before training. Thresholds are suggested on one half by family and reported on the other. No leak. |
| Feature space | Built from today's catalogue (photo components, vocabulary) and today's written profile. No labels are involved. **Caveat:** if the profile was edited after seeing results, rolling-origin numbers carry that hindsight. There is no profile history to undo it. |
| In-app observed rates (`selective.performance`) | Estimates are logged at serving time, before the decision. Prospective; no leak. |

## Added to the evaluator (`eval/selective.py`)

- **Unchanged comparator.** The plain taste ranking at the gate's own coverage, block by block. It stays as the
  baseline when labels or the gate change, so before and after are compared against the same thing.
- **Ablations with their own coverage.** Gating on the estimate alone or on raw σ(m) selects a different number of
  pieces. The report now prints that coverage beside each figure, so they are not mistaken for matched comparisons.
- **Unfamiliar-seller slice.** Pieces from sellers you had never rated before, as of each prediction. This shows
  whether the model holds up where seller familiarity cannot help.
- **Served feeds, from Discover's logs**, each with counts and 95% intervals:
  - empty feed requests by policy and mode;
  - useful discoveries per inspection: approved after first being seen, per family seen;
  - repetition: a family shown again after you had already seen it.

  Feed requests are counted per request, not per session: Discover requests a feed after every decision.

## Targets and claims

- The 80%, 90% and 95% mode targets are unvalidated aspirations. The app says so wherever it shows them.
- No uplift over the earlier ranking is claimed. The simulation in `eval/simulate_selective.py` describes a toy
  world, not anyone's taste.

## Real-data plan (for a fair first test)

1. Scan or import **150 to 300 products** from the sellers you actually buy from, across the garment types you
   wear.
2. On the Evaluate page, grade **40 to 60** uniformly sampled pieces for taste alone, before tuning anything.
   Freeze them with `python -m eval.real --freeze`. These pieces are drawn from the catalogue, not from the feed,
   so they measure coverage honestly.
3. Use Discover normally until at least 30 garment families are rated, including at least 5 approvals and 5
   rejections. Use Learning picks for some of them, so that exploration is represented.
4. Run `python -m eval.selective` and `python -m eval.real`. Both work on a copy of the database and save
   aggregates only. Read the intervals, not the point estimates. A mode meets its target only if the lower end of
   its interval clears it. With 40 to 60 grades the intervals will be wide; that is the honest answer at that
   sample size.
5. Compare the gate against the unchanged taste ranking at the same coverage, and look at the unfamiliar-seller
   slice and the served-feed rates (empty feeds, discoveries per inspection, repetition).
