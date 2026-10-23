# Phase 3: feed latency and correctness

Measured 22 October 2026 on Linux x86_64, Python 3.12.14, the lightweight image backend, the app's configured BLAS
thread limit, and disposable **synthetic** catalogues. These are server response times through FastAPI TestClient,
not browser paint times or measurements of personal taste. Machine load and hardware affect the numbers; some
validation work ran concurrently. Do not compare them directly with another machine's historical timings.

## What changed

The handoff at `d45d9cf` already remembered feed selections, rebuilt displayed cards on every request, and avoided
constructing ORM products for ranking. This follow-up:

- Shares one catalogue representation between ranking, feature building and equivalent-offer lookup.
- Assesses quality using bulk immutable evidence records instead of loading thousands of ORM products.
- Reuses each assessment until its actual inputs change: the listing's price/category/seller, its evidence, seller
  evidence, its category median, or an equivalent offer. Adding a seller claim for another listing no longer
  invalidates every assessment. Settings, seller metadata, relationship changes and database replacement remain
  conservative invalidation boundaries.
- Materialises explanations only for the displayed pieces before remembering an answer. Old lazy explanations
  otherwise kept entire catalogue matrices alive. Fit caches now release matrices from superseded feature spaces.
- Corrects the benchmark's background-refit counter (`succeeded`, not the nonexistent `done` status); a run with
  no completed refit now fails. Profiling invokes the route in the profiling thread, rather than measuring
  TestClient's wait for another thread.

The algorithm, confidence gates, precision targets, image representation and photo selection are unchanged.

## Measurements

The complete endpoint results, query counts, response sizes, after-decision timings, concurrent-scan timings and
peak process memory are in [`feed_latency_beta.jsonl`](../../backend/bench/results/feed_latency_beta.jsonl).

| Starting listings | Warm p50 / p95 | After decision p50 / p95 | With refits p50 / p95 | During scan p50 / p95 | Peak RSS |
|---:|---:|---:|---:|---:|---:|
| 1,000 | 20 / 52 ms | 78 / 96 ms | 86 / 180 ms | 0.37 / 0.62 s | 248 MB |
| 5,000 | 20 / 40 ms | 194 / 197 ms | 192 / 334 ms | 1.43 / 1.64 s | 389 MB |
| 20,000 | 24 / 28 ms | 676 / 724 ms | 698 / 1123 ms | 5.24 / 6.06 s | 924 MB |

Interpret the request types carefully:

- **Warm**: 20 repetitions with no decision or catalogue change. The remembered selection is reused; the feed gets
  a new id and fresh cards each time.
- **After decision**: 12 rounds, alternating likes and dislikes. These invalidate the choice and refit the taste
  model. A second 12-round series enables the real background calibration queue.
- **During scan**: 20 requests while another thread inserts analysed listings, writes seller claims and updates
  existing prices, committing every five listings. This intentionally stresses invalidation. The catalogue grows
  beyond the starting size; the raw result records how many listings were written.
- The endpoint table's **cold_ms** is the first request after a decision, with catalogue/model setup potentially
  already cached. It is **not** a process-restart measurement. During-scan figures show sustained catalogue changes.
- p95 is the harness's order statistic from these small samples, not a production latency guarantee.

The post-decision 2-second target must be judged from the measured post-decision series, including refits.
Continuous scanning remains the expensive path: new images still require the exact feature representation and
ranking to be rebuilt. This work does not claim that all 20,000-listing requests complete within two seconds.

## Correctness and reproducibility

`bench/feed_equivalence.py` computes fingerprints for all candidate ranking fields and explanations, the exact
feature matrix bytes, both feed selections, their confidence blocks, family offers and gate answers. A 3,000-listing
catalogue was compared against `d45d9cf` at four stages: initial, after a like, after a price change, and after deletion.
The only omitted gate field is the calibration's creation timestamp. All fingerprints match exactly:

- [`feed_equivalence_d45d9cf.json`](../../backend/bench/results/feed_equivalence_d45d9cf.json)
- [`feed_equivalence_beta.json`](../../backend/bench/results/feed_equivalence_beta.json)

This is an implementation-equivalence check on a synthetic fixture, not proof of real-world recommendation accuracy.
The cache regression tests also compare incremental assessments with a fresh calculation after price, median,
new-listing/evidence and seller/offer changes. Existing feed tests cover Undo, imports, settings, evidence edits,
reanalysis, deletion, withdrawal/refit and expired retry backoff. A weak-reference regression ensures remembered
answers do not retain an evicted catalogue's feature matrix.

Reproduce from `backend/`:

```bash
.venv/bin/python -m bench.feed_latency 1000,5000,20000 > bench/results/feed_latency_beta.jsonl
.venv/bin/python -m bench.feed_latency --profile 20000
.venv/bin/python -m bench.feed_equivalence /tmp/feed-equivalence.json
```

For a before/after comparison, run the **same** `feed_equivalence.py` script in separate processes, with `PYTHONPATH`
pointing at each checkout's `backend` directory and the same installed dependencies. Compare the two JSON files.
The latency harness uses Unix `resource` for memory accounting; use Linux for these measurements. Windows support
is checked separately by the backend CI job.
