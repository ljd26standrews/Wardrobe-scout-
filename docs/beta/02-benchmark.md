# Frozen benchmarks: format and commands

A benchmark is the set of graded garments that both evaluators measure on (`eval.real` and the representative
protocol of `eval.selective`), together with the pilot report. It is stored as data: the records and a snapshot of the
database. A list of ids or a fingerprint is not enough. Code: `backend/eval/benchmark.py`; tests:
`backend/tests/test_benchmark.py`.

## Commands (from `backend/`)

```bash
.venv/bin/python -m eval.benchmark freeze --name pilot                # pilot@v1 (refused if pilot already has a version)
.venv/bin/python -m eval.benchmark freeze --name pilot --new-version  # pilot@v2: a deliberate successor, parent recorded
.venv/bin/python -m eval.benchmark list                               # versions, hashes, how many runs used each
.venv/bin/python -m eval.benchmark verify pilot@v1                    # recompute every hash
.venv/bin/python -m eval.benchmark legacy                             # what an old ID-only freeze holds
.venv/bin/python -m eval.benchmark freeze --name rebuilt --from-legacy

.venv/bin/python -m eval.real --benchmark pilot@v1                    # trained on its snapshot (reproducible)
.venv/bin/python -m eval.real --benchmark pilot@v1 --training live [--cutoff 2026-11-01T00:00:00]
.venv/bin/python -m eval.selective --benchmark pilot@v1 [--training live]
.venv/bin/python -m eval.pilot report --benchmark pilot@v1 [--training live]
.venv/bin/python -m eval.real --freeze                                # shorthand: freeze as "default"
```

`--db PATH` selects another database file; `--data-dir DIR` selects where benchmarks and results are kept. Both can
come before or after the sub-command. On Windows use `.\.venv\Scripts\python.exe` in place of `.venv/bin/python`.
Every command works on a disposable copy of the database (SQLite online backup, source opened read-only). None of
them writes to your database.

## Layout

```
<data dir>/evaluation/benchmarks/<name>/
    v1/                 written once, then made read-only
        manifest.json
        records.jsonl
        snapshot.db
    v2/ ...
    runs.jsonl          one line per evaluation run on any version (append-only)
```

### manifest.json

| Field | Meaning |
|---|---|
| `format` | `wardrobe-scout-benchmark/1` |
| `name`, `version`, `parent` | `pilot`, 2, `pilot@v1` (a successor names its parent) |
| `created_at`, `training_cutoff` | When it was frozen. The snapshot holds everything known up to then. |
| `protocol` | Test set, approval rule (grade ≥ 2), one record per garment, held-out policy (`heldout-v1`), split rule (`sha256(seed:unit) % 2`) and seed |
| `counts` | Judgements, records, approved, merged, conflicts, missing listings, sellers, categories |
| `excluded` | Judgement ids left out as conflicting grades of one garment, or because no listing remained |
| `held_out_seeds` | Family keys, listing identities (seller, seller's id), product ids and addresses of every graded listing: what is resolved and left out of training |
| `records_sha256`, `snapshot.sha256`, `snapshot.bytes` | Hashes of the two data files |
| `content_sha256` | The benchmark's identity: hash of records, snapshot and protocol. Every report names it. |
| `data` | Database instance, revision counters, schema revision, row counts, listings by photo method, latest decision |
| `profile`, `representation`, `engine`, `labels`, `calibration_method`, `image_backend_now` | What produced it |
| `code` | Git commit (and whether the tree was dirty), and a hash of the evaluation sources |
| `legacy` | Only for `--from-legacy`: what the old freeze held and what could not be recovered |

### records.jsonl

There is one line per graded garment. Agreeing grades of one garment are merged, keeping the first grade.
Disagreeing ones are left out and listed under `excluded`.

| Field | Meaning |
|---|---|
| `record`, `family`, `unit`, `split` | Record number, family key when frozen, split unit (garments that may become one share it), half (0/1) |
| `taste`, `approved`, `grades`, `judgement_ids`, `judged_at` | The grade used, approval, every grade merged into it, when |
| `flags`, `sampled_by` | Owned similar, too expensive, quality unclear (never part of taste); how it was sampled |
| `listing` | Stable identity of the scored listing: `product_id`, `seller_id`, `external_id`, `source_url` |
| `members` | The listings of its family when frozen |
| `content` | Fingerprint of the listing's content when frozen (attributes, photo vector and method) |
| `snapshot` | The piece as graded (title, category, colours, seller, price, address) |

### runs.jsonl

Each line holds the benchmark and its hash, plus:

- the evaluator;
- training data (`snapshot` or `live`) and cutoff;
- the training data's identity (revisions, counts, photo methods);
- engine, labels, calibration method and photo analysis;
- a hash of the settings;
- the code;
- the result file.

## Guarantees, and how they are kept

- **No substitution.** Evaluation reads `records.jsonl`, never the judgements table. An Undo, a re-grade, a judgement
  id reused by SQLite, an import or an edited listing cannot change what is measured (`test_benchmark.py`: results
  identical before and after all of these).
- **Never overwritten.** A second freeze of a name is refused. `--new-version` creates a successor. The files are made
  read-only, and every use recomputes the hashes and refuses a changed file.
- **A frozen population is not a frozen experiment.** `--training snapshot` repeats the experiment exactly, with the
  same training data. `--training live` measures the same graded garments with today's training data, and each run
  declares which one it used. In live mode every record must still resolve, by seller and seller's id (or by
  address), to exactly one listing. Records that have since become one garment, or whose split units were joined by a
  later match, make the run refuse; a successor version is the way forward. Listings whose content changed are
  listed.
- **Legacy freezes are reported, not used.** `evaluation/frozen_test_set.json` (judgement ids only) cannot be
  verified. `eval.real` says so, and `legacy` lists ids that are gone and ids now naming grades made after the freeze.
  `--from-legacy` rebuilds a benchmark from the grades that existed when it was frozen, and its manifest says what
  could not be recovered.
