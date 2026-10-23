# Running a pilot on your own data

The pilot answers one question with your own judgements: does selective recommendation find pieces you like
often enough to trust, and how sure can anyone be? Nothing in it invents, infers or simulates a grade. It uses your
grades and decisions as stored, and it refuses a number when the sample is too small.

What it needs (`eval/pilot.py` checks each one):

| | Wanted | Why |
|---|---|---|
| Readable listings | 150–300 | A title, a price and an analysed photo. Fewer give too few undecided pieces to sample from. |
| Graded garments, held out | 40–60 | Graded on the Evaluate page. These are drawn uniformly from undecided readable listings, so the model never chose them. |
| Rated decisions on Discover | 60 or more | These train the model and are collected separately from the grades. Calibration alone needs 30. |
| Decisions on Learning picks | 10 or more | Exploration reaches pieces the ranking alone would not show you. |

## Steps

1. **Catalogue.** Scan your sellers (Sellers → Scan all) until Settings shows 150–300 analysed listings. Check which
   photo analysis is in use: Settings shows "CLIP" or "lightweight". Every report states it, and results under one
   say nothing about the other.
2. **Grade first.** Open Evaluate and grade 40–60 pieces for the look alone (0 not for me, 1 unsure, 2 like,
   3 love). Price, size and quality have their own tick boxes and never change the grade.
3. **Freeze the grades** before looking at any result:

   ```bash
   cd backend
   .venv/bin/python -m eval.benchmark freeze --name pilot
   ```

   This writes `pilot@v1`. The folder holds the graded garments as records, a snapshot of the database and a manifest
   with hashes. It is read-only and is never overwritten ([benchmark format](02-benchmark.md)).
4. **Use Discover** as you normally would, including the Learning picks tab, until you have at least 60 rated
   decisions. Discover may show a piece you graded. Decide on it as you like: anything linked to a graded garment is
   left out of training when it is measured.
5. **Check progress** at any time:

   ```bash
   .venv/bin/python -m eval.pilot plan
   ```

6. **Report** (Markdown printed and saved under `<data dir>/evaluation/pilot/`, JSON beside it):

   ```bash
   .venv/bin/python -m eval.pilot report --benchmark pilot@v1 --training live
   ```

   `--training live` trains on everything you have decided by now. That is usually what you want after step 4,
   because the frozen snapshot predates those decisions. `--training snapshot` (the default) repeats a frozen
   experiment exactly. Add `--cutoff 2026-11-01T00:00:00` to train only on what was known before a date. Each run is
   logged in `evaluation/benchmarks/pilot/runs.jsonl`, so reuse of the same test set stays visible.

On Windows (PowerShell, from the `backend` folder), use `.\.venv\Scripts\python.exe` instead of
`.venv/bin/python`, for example `.\.venv\Scripts\python.exe -m eval.pilot plan`.

If you grade more later, freeze a successor (`freeze --name pilot --new-version` makes `pilot@v2`) instead of changing
`pilot@v1`.

## What the report contains

- **Requirements**: the table above, with what is still short.
- **Sample size**: the 95% Clopper–Pearson lower bound when every selected piece is approved. It is 88.4% at 30,
  94.0% at 60, and reaches 95% first at 72. A 95% target is not met with fewer selected pieces, however good the
  point estimate.
- **Graded garments**: per mode, the gate's precision with a 95% interval and its coverage. It compares, at the same
  coverage (each takes as many pieces as the gate did), your written profile alone, the learned taste ranking and the
  full Discover score, which includes quality and value. Calibration diagnostics come from the held-out grades, which
  were never used to fit anything. Slices cover garment type and sellers you had never rated. Thresholds are chosen
  on one half of the graded garments and measured on the other half.
- **Discover decisions in time order**: each one is predicted by a model fitted only on earlier decisions.
- **What Discover served and you saw**:
  - empty-feed answers;
  - gate coverage from the feed logs;
  - approvals after first seeing a piece, per exposure;
  - repetition;
  - variety (how many garment types you saw, and how evenly).

  An exposure means a piece was on screen long enough, or you decided about it while it was on screen
  (`exposure-v2`). Earlier "rendered" records are reported but never used.
- **Taste and quality**: grades are about the look alone. The quality and price flags are counted separately.
- **What it does not show**: it covers the graded sample only, and one person. Accuracy for you is measured,
  never proven.

The [example report](example-pilot-report-synthetic.md) was produced on the synthetic test catalogue, to show the
layout and the refusals. It is not a result.
