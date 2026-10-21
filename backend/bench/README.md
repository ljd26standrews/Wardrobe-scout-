# Scanning benchmarks

Reproducible measurements of catalogue scanning: speed, requests, bandwidth, CPU, memory, size-chart
exclusion, duplicate detection and recommendation quality, for the original and the optimised scanner.
Results and interpretation: [`RESULTS.md`](RESULTS.md).

## What is simulated, and how

- **Real photographs.** Garment photos come from the CC0 [clothing-dataset-small](https://github.com/alexeygrigorev/clothing-dataset-small)
  (3,781 photos, 10 classes, many taken on carpets, beds and floors).

  ```bash
  git clone --depth 1 https://github.com/alexeygrigorev/clothing-dataset-small.git backend/bench/data/clothing-dataset-small
  ```

  Set `WS_BENCH_PHOTOS` to use another location. The repository only contains 24 resized photos, in
  `tests/fixtures/photos`, which the tests use.
- **Galleries** (`catalogue.py`, `render.py`). Each product gets a gallery built from one real photo:
  - Hero view.
  - Mirrored or re-framed views.
  - Close-up detail crops.
  - Re-encoded near-duplicates.
  - Seller watermarks.
  - Measurement arrows drawn on the garment photo.
  - Collages.
  - Rendered Chinese and English size tables, measurement diagrams, photographed size charts and inverted (dark) charts.
  - Shop notices and a seller banner reused across listings.

  Galleries average about 9 images, capped at 16. 10% of products have a size chart as the *first* image.
- **Duplicates.** 8% of products have a **colourway sibling** (the same photos with the hue rotated). These are
  different products and must not be merged. 10% are **re-posted by another seller** (the same photos, shuffled and
  re-encoded). These should be matched.
- **Titles are deliberately opaque** (`【code】新款 ¥price`). The simulated shoppers' taste can then only be learned from
  photos, which makes the recommendation-quality score sensitive to the image pipeline.
- **Server** (`server.py`). This is a real HTTP server in its own process (so image rendering does not count
  towards the scanner's CPU). It serves Yupoo-like markup: listing pages with 40 albums each, album pages, and
  `photo.yupoo.com/<seller>/<key>/{big,medium}.jpg` variants at 1280 px and 640 px.
  - **Network model.** Each response takes `scale × (rtt + bytes/bandwidth)`, with rtt = 120 ms and bandwidth = 3 MB/s.
  - **Faults.** These are optional: 5xx errors, missing images, truncated HTML and slow responses.
  - **Conditional requests.** ETag and If-None-Match are supported.
- **Runner** (`run.py`). It drives the app's own `PoliteClient` and `scan_seller`/`scan_many` against that server,
  using a transport that keeps each original Host header.
  - The politeness delay is the app default of 2.0 s multiplied by `--scale`, as are the network times.
  - **Timers.** The runner wraps the key functions to report time spent in HTTP, politeness waiting, parsing, image
    decoding, analysis, embedding, deduplication and database commits. These are thread-seconds: they overlap when
    work is concurrent.
  - **Real-time estimate.** `politeness_floor_s` is the minimum real-world duration at the real 2 s delay. The original
    scanner is serial, so its floor is the *sum* of requests × 2 s. The optimised scanner runs hosts in parallel, so its
    floor is the busiest host's requests × 2 s. `--scale 1.0` runs in real time; it is used for the 50-product check.
- **Scoring against ground truth** (`score.py`):
  - completeness;
  - which images fed each product vector (contamination by charts and text);
  - duplicate recall and false matches;
  - leave-one-out 5-nearest-neighbour garment-class accuracy of the product vectors.
- **Recommendation quality** (`quality.py`). Two simulated shoppers run 8 rounds of the real Discover feed.
  `dark_outerwear` likes coats, long sleeves and trousers in dark colours. `light_summer` likes tees, shorts, skirts and
  dresses in light colours. Their taste depends only on the photos. The score is precision@10.
- **Selection ablation** (`ablate_selection.py`). Compares how many and which photos should feed a product vector.
- **Detector evaluation** (`eval_detector.py`). Runs the size-chart detector over all 3,781 real photos and over
  rendered charts.

## Running

```bash
cd backend
.venv/bin/python -m bench.run --products 300 --repeat changed --quality --out /tmp/opt300.json
.venv/bin/python -m bench.eval_detector
.venv/bin/python -m bench.ablate_selection --products 1000

# Everything, against a checkout of the original code (python -m imports `app` from the working
# directory, so the suite runs baseline measurements from inside that checkout):
git worktree add /tmp/ws-original <original-commit>
BASELINE=/tmp/ws-original/backend OUT=/tmp/wsbench ./bench/suite.sh
.venv/bin/python -m bench.compare /tmp/wsbench/base1000.json /tmp/wsbench/opt1000.json
```

## Limitations

- **Synthetic catalogues.** Live Yupoo and Weidian catalogues could not be reached from the environment these were
  measured in. Real albums may order photos differently, use other size-chart designs, or re-post photos with a
  different cover.
- **Real versus synthetic images.** The garment photos are real; size charts, notices and diagrams are rendered. The
  detector's false-positive rate is measured on real photos. Its recall is measured only on rendered charts.
- **CLIP.** CLIP could not be measured here (its weights could not be downloaded). All embedding results use the
  built-in lite image features.
- **Time scaling.** Wall-clock times at `--scale < 1` mix scaled network waits with unscaled CPU time. Request counts,
  bytes, CPU seconds and the politeness floor are the comparable numbers.
