# Performance: measured, profiled, cached by data revision

Everything here was executed on this branch, on a synthetic catalogue, on one machine. The timings depend on
that machine; compare request kinds and sizes with each other, not with your computer.

## Method

`backend/bench/feed_latency.py` builds a fresh app on a temporary database for each size (1,000, 5,000 and
20,000 pieces), each in its own process so memory does not carry over. The catalogue is meant to look like a
real one: three photo records per piece (an analysed garment photo, a second view, a size chart), parsed
attributes, prices in yuan, pounds and unstated currencies, twelve sellers with some seller evidence, 10% of
pieces with evidence, 10% in families of two or three listings, and 400 decisions with the reasons people give,
with one calibration fitted before timing.

Each request kind is measured through the real app:

- **cold**: the first request after the data changed (one more decision), so anything keyed by decisions misses;
- **warm**: 20 repeats with nothing changed, as p50 and p95;
- **SQL**: statements executed for one warm request;
- **KB**: response size;
- **peak RSS**: the process's peak memory at the end.

```bash
cd backend
.venv/bin/python -m bench.feed_latency 1000,5000,20000      # one JSON line per size
.venv/bin/python -m bench.feed_latency --profile 20000      # where the time goes in one feed request
```

"Before" is commit `ccca8ad` (all correctness fixes, no caches) with the same benchmark script copied in.
The historical figure of 4.6 s for a 20,000-piece Balanced feed was re-measured first with the older
`bench.selective` harness at 4.52 s (on the earlier, faster machine), so the starting point matches the earlier
report. That harness times ranking and the gate alone; the figures below time the whole request, so they read
higher.

## Results

Both runs were made on this machine, one after the other, with the same script: "before → after" in every cell.
An earlier pair of runs on a faster machine (before the container restarted) showed the same pattern after the
first round of changes; only same-machine comparisons are reported here.

The raw output is in `backend/bench/results/feed_latency_before_ccca8ad.jsonl` and `feed_latency_after.jsonl`.

In short, on this machine:

- A Discover feed takes about **1.3 s** warm at 5,000 pieces (was 2.3 s) and about **6 s** at 20,000 (was 9.7 s).
- Browse takes 1.1 s at 5,000 (was 2.7 s) and 4.5 s at 20,000 (was 11.0 s).
- The first product page after a decision takes 0.14–0.21 s (was 1.9 s at 5,000 and 7.8 s at 20,000): a
  decision no longer invalidates work that does not depend on decisions.
- SQL statements per feed fell from 98–143 to 61–62, and peak memory at 20,000 pieces from 837 MB to 555 MB.
- Feeds at 20,000 pieces are still slow. That is not solved.

### 1,000 pieces

| Request | Cold, ms | Warm p50, ms | Warm p95, ms | SQL statements | Response, KB |
|---|---|---|---|---|---|
| Discover feed, Balanced | 661 → 496 | 495 → 308 | 686 → 490 | 104 → 61 | 80 → 80 |
| Discover feed, High confidence | 616 → 361 | 453 → 308 | 748 → 510 | 98 → 61 | 60 → 60 |
| Learning picks | 487 → 506 | 454 → 336 | 600 → 579 | 106 → 62 | 90 → 90 |
| Browse (best match) | 650 → 563 | 526 → 262 | 672 → 448 | 47 → 29 | 112 → 112 |
| Product page | 590 → 179 | 145 → 159 | 209 → 183 | 55 → 56 | 3.7 → 3.7 |
| Saved | 23 → 19 | 30 → 19 | 31 → 24 | 21 → 15 | 9.8 → 9.8 |
| Insights | 722 → 534 | 369 → 329 | 573 → 500 | 92 → 72 | 16 → 16 |
| Selective status | 125 → 142 | 78 → 91 | 101 → 151 | 26 → 26 | 3.2 → 3.2 |

Peak memory: 230.4 MB → 215.6 MB. Building the catalogue took 3.5 s and 4.0 s.

### 5,000 pieces

| Request | Cold, ms | Warm p50, ms | Warm p95, ms | SQL statements | Response, KB |
|---|---|---|---|---|---|
| Discover feed, Balanced | 2,956 → 1,484 | 2,266 → 1,327 | 2,484 → 1,511 | 111 → 61 | 80 → 80 |
| Discover feed, High confidence | 2,555 → 1,325 | 2,188 → 1,305 | 2,621 → 1,601 | 109 → 61 | 73 → 73 |
| Learning picks | 3,549 → 1,689 | 2,308 → 1,314 | 2,906 → 1,588 | 107 → 62 | 70 → 70 |
| Browse (best match) | 3,200 → 915 | 2,714 → 1,100 | 3,165 → 1,310 | 63 → 29 | 113 → 113 |
| Product page | 1,915 → 137 | 262 → 139 | 329 → 201 | 55 → 56 | 3.6 → 3.6 |
| Saved | 73 → 16 | 66 → 18 | 75 → 21 | 21 → 15 | 9.8 → 9.8 |
| Insights | 3,269 → 1,199 | 2,289 → 1,257 | 2,655 → 1,369 | 99 → 72 | 17 → 17 |
| Selective status | 95 → 86 | 99 → 78 | 129 → 126 | 26 → 26 | 3.2 → 3.2 |

Peak memory: 352.5 MB → 289.4 MB. Building the catalogue took 7.7 s and 8.4 s.

### 20,000 pieces

| Request | Cold, ms | Warm p50, ms | Warm p95, ms | SQL statements | Response, KB |
|---|---|---|---|---|---|
| Discover feed, Balanced | 12,001 → 8,725 | 9,706 → 6,078 | 10,834 → 6,711 | 143 → 61 | 83 → 83 |
| Discover feed, High confidence | 12,761 → 4,804 | 9,328 → 5,560 | 10,691 → 6,241 | 143 → 61 | 81 → 81 |
| Learning picks | 12,559 → 6,246 | 9,320 → 5,892 | 10,571 → 6,948 | 141 → 62 | 76 → 76 |
| Browse (best match) | 13,411 → 4,968 | 10,984 → 4,496 | 11,802 → 4,951 | 123 → 29 | 113 → 113 |
| Product page | 7,812 → 214 | 500 → 222 | 720 → 289 | 55 → 56 | 3.8 → 3.8 |
| Saved | 139 → 21 | 134 → 19 | 185 → 22 | 21 → 15 | 9.8 → 9.8 |
| Insights | 13,630 → 6,266 | 9,756 → 5,486 | 11,216 → 5,950 | 131 → 72 | 16 → 16 |
| Selective status | 96 → 99 | 94 → 96 | 137 → 131 | 26 → 26 | 3.2 → 3.2 |

Peak memory: 837.3 MB → 554.5 MB. Building the catalogue took 24.5 s and 26.0 s.

## Where the time went, and what changed

A profile of one warm 20,000-piece Balanced feed, taken once the revision-keyed caches were in place (on the
earlier machine; cProfile adds overhead, so the shares are what matter), showed where the rest went:

| Part | Share | What it was | Change |
|---|---|---|---|
| Loading candidates | ~30% | Every active listing as an ORM object, plus all their evidence | Evidence is now loaded only for pieces whose quality is not already cached, in batches |
| The earlier engine's attribute score | ~17% | Computed for every piece under the Bayesian engine, then discarded | No longer computed under the Bayesian engine |
| "Already own" checks | ~8% | Repeated for the same pieces by ranking and by the gate | Remembered within a request |
| Category price medians | ~5% | Recomputed twice per request | Cached until listings or rates change |
| Feature space, quality, written-profile scores | (first round) | Rebuilt after every decision because "seen" flags changed the cache key | Keyed by content revisions that decisions do not touch |
| Browse | — | Photos and evidence for every listing on every request, though it shows 36 | Photos only for the page shown; evidence only where needed |

What remains, from a profile of one warm 20,000-piece Balanced feed after these changes: loading every candidate
from the database as an object, about 37% (much of it decoding stored lists such as colours and sizes); the
per-piece ranking loop, about 42%; and the confidence gate, about 13%. The next step would be a per-piece feature
table kept current by the same revision counters, so a feed reads columns instead of building 20,000 objects.
That was not attempted here.

## Caches and invalidation

All shared caches are keyed by **data revisions** (`backend/app/services/revisions.py`): a counter per table,
bumped by database triggers on every insert, delete and content update, plus a random instance number per
database (renewed on full restore). Because the triggers live in the database, every writer invalidates the
caches, including imports, backups, scans and background jobs; no code path has to remember to clear anything.
The products counter only counts content columns, so marking a piece as seen or recording a decision does not
invalidate content caches.

| Cache | Keyed by | Invalidated by |
|---|---|---|
| Taste feature space | products, wardrobe attributes, profile, photo method | product edits, scans, imports, wardrobe changes, a new written profile or photo method |
| Quality assessments | products, evidence, relations, settings, rates | evidence edits, match decisions, product updates, settings, rate changes |
| Written-profile scores | products, profile | product edits, a new written profile |
| Category price medians | products, rates | product edits, rate changes |

`backend/tests/test_caches.py` checks each: feedback and Undo leave them alone; evidence edits, matching, imports,
product updates, settings and rates invalidate them; one database's cache is never served for another; a
restored backup starts fresh.

## Older answers never restore decided pieces

Separately from caching, Discover keeps the pieces you decided on during the session and filters every feed
answer through them, so an answer computed before a decision cannot bring the piece back. The end-to-end
timing tests exercise this with answers held back by 2.5 seconds and a slow save.

## Photo analysis in use

`/api/status` reports which photo analysis is in use (CLIP or the lightweight features) and how many listings
were analysed with it or with another method. Settings shows this. Vectors from different methods are not
comparable: listings analysed with another method count as unanalysed until **Re-analyse all photos**, and
match estimates fitted under another method are withdrawn (the calibration records the photo method) and
refitted automatically.
