# Scanning benchmark results

Every number here comes from a run whose raw output is in [`results/`](results/). The method is in
[`README.md`](README.md).

**Read this first:**

- **Synthetic catalogues, not live sellers.** The catalogues are Yupoo-like, built from real CC0 garment photos plus
  rendered size charts and notices, and served by a local server with modelled latency.
- **Not a measure of your taste or garment quality.**
- **Lite image features only.** Optional CLIP was not available in the measurement environment.
- **Which code was measured:**
  - The *baseline* is the scanner at commit `2bca6b3` (`audit/phase-1`).
  - *Optimised* is the scanner at `f1e9ecb` (`perf/scanning`).
  - Each file records which `app` package ran (`app_code`).
  - Later reliability fixes (branch `audit/reliability-followup`) don't change the scanning strategy. Two of
    them were re-measured, each on one catalogue; see the two "Tranche 2 re-check" sections below:
    - duplicate counting;
    - pages that arrive cut short.
- **How to read the times:**
  - Wall-clock times at `--scale < 1` mix scaled network waits with unscaled CPU time.
  - The comparable figures are requests, bytes, CPU seconds, and the **minimum real-world time** (requests × the
    real 2 s politeness delay). That minimum is summed across hosts for the serial baseline, and taken over the
    busiest host for the optimised scanner, which runs hosts in parallel.

## Real-time check (50 products, real 2 s delay, modelled network)

| | Baseline | Optimised |
|---|---|---|
| HTTP requests | 493 | 180 |
| Photo data | 51.7 MB | 5.5 MB |
| **Measured wall-clock** | **881 s** | **249 s** |
| Predicted minimum (requests × 2 s) | 986 s serial | 248 s busiest host |
| CPU time | 15.8 s | 5.2 s |

The optimised scan took exactly as long as the image host's politeness delays allow. The baseline finished faster
than its serial estimate because network transfer overlaps the delay.
Files: `base50_realtime.json`, `opt50_realtime.json`.

## First scan, by catalogue size (default settings, same politeness)

| Products | Requests (base → opt) | Photo MB | Min. time at 2 s/host | CPU s | Peak memory MB |
|---|---|---|---|---|---|
| 50 | 493 → 180 | 51.5 → 5.5 | 16 min → 4 min | 15.6 → 4.6 | 209 → 222 |
| 300 | 2,916 → 965 | 305 → 26 | 97 min → 22 min | 95.5 → 26.7 | 220 → 250 |
| 1,000 | 9,775 → 2,883 | 1,026 → 75 | 5.4 h → 62 min | 356 → 78 | 242 → 316 |
| 3,000 | 29,623 → 8,239 | 3,098 → 209 | 16.5 h → 2.9 h | 1,849 → 225 | 283 → 396 |

The optimised scanner uses **more peak memory** (+13% to +40%).

Deduplication time grows quadratically in the baseline and roughly linearly in the optimised scanner:

| Products | Baseline | Optimised |
|---|---|---|
| 1,000 | 49.7 s | 1.6 s |
| 3,000 | 402 s | 6.1 s |

Files: `base*.json`, `opt*.json`.

## What feeds the taste model, and duplicate detection

| Measure (catalogue size) | Baseline | Optimised (default) | Optimised, `WS_ANALYSIS_IMAGES=2` |
|---|---|---|---|
| Product vectors including a size chart or text graphic (1,000) | 86.3% | 0.4% | 1.2% |
| False strong "same item" matches between unrelated products (1,000) | 1,151 | 0 | 6 |
| False weak photo matches between unrelated products (1,000) | 53,291 | 0 | 10 |
| Colourways wrongly matched (1,000) | 45 of 72 | 0 | 10 |
| **True cross-seller duplicates found (1,000)** | **81/83** | **54/83** | 78/83 |
| True duplicates found (300, seed 7) | 29/31 | 25/31 | – |
| True duplicates found (3,000) | 254/257 | 132/257 | – |
| Visual 5-NN class agreement (1,000) | 0.367 | 0.423 | 0.392 |

- **Duplicate recall is lower with the default settings.** Analysing one photo per product means a re-post whose
  first photo is a different view is often missed. At 3,000 products the deep-analysis budget (300 products) was
  reached.
- **The "thorough" setting** recovers most of that recall: 78/83 duplicates, for 1.9× the photo requests and
  1.57× the total requests. It reintroduces a few false matches.
- **Baseline precision was worse.** Most of the baseline's apparent duplicates were false: shared size charts and
  shop banners.

Files: `base1000.json`, `opt1000.json`, `opt1000_thorough.json`, `base300_s7.json`, `opt300_s7.json`,
`base3000.json`, `opt3000.json`.

## Recommendation quality (simulated shoppers): no demonstrated difference

Two simulated shoppers whose taste depends only on the photos ran 8 rounds of the real Discover feed. The table shows
precision@10, the mean of the two shoppers.

| Run | Baseline | Optimised |
|---|---|---|
| 300 products, seed 7 | 0.306 | 0.382 |
| 300 products, seed 8 | 0.375 | 0.369 |
| 300 products, seed 9 | 0.344 | 0.369 |
| 300 products, mean of 3 seeds | 0.342 | 0.373 |
| 1,000 products | **0.393** | **0.369** |
| 1,000 products, thorough setting | – | 0.375 |

Individual shopper scores swing from 0.275 to 0.462 between runs. With this few runs, the difference is within noise:
**these results do not show that the optimised scanner improves or harms recommendations.** That needs real
products and your real judgements.

## Repeat scans

| Changed catalogue (≈6% new, 5% repriced, 3% removed) | Baseline | Optimised |
|---|---|---|
| 300 products: requests / min. time | 233 / 466 s | 77 / 44 s |
| 300 products: removed products still shown active | 11 of 11 | 0 of 11 |
| 1,000 products: requests / min. time | 610 / 1,220 s | 298 / 254 s |
| 1,000 products: new products found | 58 of 63 | 63 of 63 |
| 1,000 products: removed products still shown active | 27 of 27 | 0 of 27 |

- **New albums.** The baseline stops after 40 already-known albums, so it misses new albums inserted further down a
  catalogue that isn't in date order. The optimised scanner reads the whole listing.
- **Unchanged catalogue.** At 3,000 products, the optimised scanner made 79 requests, with a minimum time of 54 s.
  The baseline's equivalent at 1,000 products made 6 requests, but only because it stopped early.

## Fault injection (300 products)

The server returned 5% HTTP 503 errors, 5% missing images, 3% truncated album pages and 2% slow responses.

- **Both scanners:** found all 300 products and built a vector for each.
- **Optimised scanner:** finished in 899 requests, against 2,864 for the baseline.
- **Duplicate recall** fell for both, more for the optimised scanner (12 of 31, against 27 of 31), because missing
  images remove matching evidence.

Files: `base300_faults.json`, `opt300_faults.json`.

### Tranche 2 re-check: pages that arrive cut short

The fault settings were the same as above. Each version was run three times, interleaved:

- **Before:** `9028daf`, which stored a cut-short page as it arrived.
- **After:** `43977d2`, which keeps stored values for what a cut page did not reach and reads it again.

| | Before | After |
|---|---|---|
| First-scan requests (three runs) | 756, 759, 766 | 788, 868, 749 |
| Requests in the follow-up analysis pass | 2, 0, 0 | 16, 16, 20 |
| Products with a photo vector | 300 in every run | 300 in every run |
| True duplicates found (strong), of 31 | 1, 3, 1 | 1, 6, 1 |
| Deep duplicate checks | 12, 12, 14 | 17, 30, 11 |

- **The extra requests are the intended cost.** The 16–20 requests in the follow-up pass re-read the roughly 9 album pages that arrived cut short, plus their photos.
- **Single fault-injection runs can't be compared.** Duplicate recall varies more between runs of the same code than between versions:
  - here: 1–6 of 31 across six runs;
  - the single run reported above: 12 of 31;
  - one uncontrolled pair run during development: 9 and 3.

  Random faults land on different requests each time, and that changes which duplicate checks happen.

Files: `tranche2_faults_prev_{1,2,3}.json`, `tranche2_faults_new_{1,2,3}.json`.

## CPU: limiting numpy's maths threads

The same 3,000-product scan (optimised code, scale 0.01) was run with the library default and with BLAS limited to
one thread:

| | Library default | One thread |
|---|---|---|
| CPU time | 669 s | 221 s |
| Wall-clock | 405 s | 343 s |

This is why `WS_BLAS_THREADS` defaults to 1. Files: `blas_default_threads_opt3000.json`, `blas_one_thread_opt3000.json`.

## Size-chart detector

Measured with `python -m bench.eval_detector --n-rendered 1200`; output in `detector_eval.txt`.

| Images (all at the 640 px size a scan downloads) | Wrongly excluded or correctly excluded |
|---|---|
| Real garment photographs (3,781) | 1 wrongly excluded (0.03%) |
| Rendered garment variants: watermarks, measurement arrows, collages, close-ups (1,200) | 0 wrongly excluded |
| Rendered size tables, inverted charts, shop notices | 100% correctly excluded |
| Measurement diagrams | 97% correctly excluded |
| Photographed charts | 81.5% correctly excluded |

Positives are rendered, not taken from real sellers. Real chart designs may differ.

## Photo-selection ablation (1,000 products, offline)

From `python -m bench.ablate_selection`; raw rows in `ablation_selection_1000.json`. These use lite features; the
taste AUC is the AUC of a logistic model on 16 PCA components.

| Strategy | Photos fetched per product | Vectors with a chart or text | 5-NN class agreement | Taste AUC |
|---|---|---|---|---|
| Baseline (first 12 images) | 8.74 | 86.3% | 0.354 | 0.753 |
| Every garment photo | 8.90 | 6.6% | 0.384 | 0.766 |
| Progressive, 1 view (default) | 1.13 | 0.8% | 0.410 | 0.772 |
| Progressive, 2 views | 3.13 | 1.1% | 0.384 | 0.771 |

## Tranche 2 re-check: counting photographs, not files (300 products, seed 7)

The tranche 2 comparison fixes changed how shared photos are counted. One photo listed at two sizes, or re-encoded, now counts once, using the same "same photograph" rule as matching across listings.

The same scan was run twice on the tranche 2 code (`audit/reliability-followup`). The only difference was `dedup.py`, which was swapped for the previous version (`da62794`) in the second run.

| | Previous photo counting | Tranche 2 |
|---|---|---|
| True duplicates found (strong) | 25/31 | **23/31** |
| True duplicates with any photo match | 26/31 | 26/31 |
| False matches, colourways wrongly matched | 0, 0 | 0, 0 |
| Requests, photo MB | 965, 26.1 | 965, 26.1 |

- **Why two pairs dropped.** In each of the two pairs no longer called "shared photos", the albums hold **one** source photograph plus near-identical copies of it. The benchmark generates these deliberately as `neardup` images.
  - The previous code counted those copies as 5 and 2 matching photos.
  - The documented rule requires two *different* photographs.
  - Both pairs are still shown, as a "one similar photograph" hint.
- **Not a measured gain.** The strong tier is now more conservative, so this is a recall cost, not a measured improvement.
- **Repeat runs.** Paired runs kept for inspection gave the same 25 → 23.

Files: `tranche2_opt300_s7.json`, `tranche2_opt300_s7_previous_dedup.json`.

## Not measured

- live seller sites;
- CLIP or other embedding models;
- Windows;
- PostgreSQL;
- long-running scheduling;
- concurrent interactive use during a large scan (beyond the lock regression test).
