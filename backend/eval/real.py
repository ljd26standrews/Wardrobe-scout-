"""Measure the recommender against your own judgements (the Evaluate page).

Usage (from backend/):
    .venv/bin/python -m eval.real --benchmark pilot@v1    # measure on a frozen benchmark version (eval/benchmark.py)
    .venv/bin/python -m eval.real --benchmark pilot@v1 --training live [--cutoff 2026-11-01]
                                                          # the same frozen garments, today's training data
    .venv/bin/python -m eval.real                         # your current grades, not frozen (while collecting)
    .venv/bin/python -m eval.real --freeze [--new-version] # freeze the current grades as benchmark "default"
    .venv/bin/python -m eval.real --db PATH               # another database file (default: this installation's)

What it does
------------
1. Copies your database (SQLite online backup, source opened read-only) and works only on the copy.
2. Test set: your judgements - uniformly sampled pieces, one per product family, graded for taste alone
   (0 not for me, 1 unsure, 2 like, 3 love). With a benchmark, its frozen records (never the judgements table, so an
   Undo, a re-grade or a reused id changes nothing) and the version's hash, named in the report.
3. Training data: everything except what is linked to a graded garment (decisions under any of its listings or
   addresses, wardrobe pieces linked to it, inspiration photos that are its photographs: services/provenance.py),
   deleted from the copy before any method is fitted, so no test label leaks into training. Grades about one garment
   are one record (eval/heldout.py: agreeing grades merged, disagreeing ones left out and reported).
4. Each method ranks the judged pieces, and the ranking is compared with your grades:
   NDCG@10 (gain 2^grade - 1), precision@10 (share graded like or love) and liked pieces in the top 20.
5. Uncertainty: 95% bootstrap intervals over test families, and paired differences against the current
   engine. Pieces from sellers you have never decided on are also reported separately when there are enough.

Methods: random (expected over shuffles), profile (your written preferences only, no learning), legacy (the
pre-audit engine) and bayes (the current default), each by the full Discover score and by taste alone.

It reports nothing with fewer than 40 judgements, 3 sellers or 5 liked pieces: below that any difference is
noise. These are measurements on your judged sample; they say nothing about pieces unlike it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
import statistics as st
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from . import benchmark as bm

K = 10
MIN_JUDGEMENTS, MIN_SELLERS, MIN_LIKED = 40, 3, 5
BOOTSTRAP = 1000
RANDOM_SHUFFLES = 200


# ----------------------------------------------------------------- metrics
def _dcg(gains: list[float]) -> float:
    return sum(g / math.log2(r + 2) for r, g in enumerate(gains))


def ranking_metrics(order: list, grades: dict) -> dict:
    """order: item keys best first; grades: key -> 0..3."""
    ideal = _dcg(sorted((2**g - 1 for g in grades.values()), reverse=True)[:K])
    top = order[:K]
    return {
        "ndcg@10": _dcg([2 ** grades[i] - 1 for i in top]) / ideal if ideal else 0.0,
        "p@10": sum(grades[i] >= 2 for i in top) / len(top) if top else 0.0,
        "liked@20": float(sum(grades[i] >= 2 for i in order[:20])),
    }


def scored_metrics(scores: dict, grades: dict) -> dict:
    """``ranking_metrics`` of ordering by ``scores``, in expectation over the order of tied scores. Exact: every
    place in a tied block holds the block's average gain, so no method gains or loses from how ties happen to be
    broken (a constant score is exactly the random baseline)."""
    keys = sorted(grades, key=lambda i: -scores[i])
    gain: list[float] = []  # expected 2^grade - 1 at each place
    liked: list[float] = []  # expected "graded like or love" at each place
    i = 0
    while i < len(keys):
        j = i
        while j < len(keys) and scores[keys[j]] == scores[keys[i]]:
            j += 1
        block = keys[i:j]
        gain += [st.mean(2 ** grades[k] - 1 for k in block)] * len(block)
        liked += [st.mean(grades[k] >= 2 for k in block)] * len(block)
        i = j
    ideal = _dcg(sorted((2**g - 1 for g in grades.values()), reverse=True)[:K])
    return {
        "ndcg@10": _dcg(gain[:K]) / ideal if ideal else 0.0,
        "p@10": sum(liked[:K]) / len(liked[:K]) if liked else 0.0,
        "liked@20": float(sum(liked[:20])),
    }


def random_metrics(grades: dict, rng: random.Random | None = None, shuffles: int = RANDOM_SHUFFLES) -> dict:
    """The random ranking's metrics, exactly (its expectation: every piece tied)."""
    return scored_metrics(dict.fromkeys(grades, 0.0), grades)


def evaluate_scores(methods: dict[str, dict], grades: dict, seed: int = 0, units: dict | None = None) -> dict:
    """Point estimates, bootstrap intervals over garments and paired differences against 'bayes'. ``units``: the
    resampling unit of each piece (garments that may become one are resampled together); default, each piece."""
    rng = random.Random(seed)
    point = {"random": random_metrics(grades, rng)} | {m: scored_metrics(s, grades) for m, s in methods.items()}
    clusters: dict = {}
    for k in grades:
        clusters.setdefault((units or {}).get(k, ("piece", k)), []).append(k)
    groups = list(clusters.values())
    boot: dict[str, list[dict]] = {m: [] for m in point}
    for _ in range(BOOTSTRAP):
        sample = [k for c in (rng.choice(groups) for _ in groups) for k in c]
        g = {(k, n): grades[k] for n, k in enumerate(sample)}
        boot["random"].append(random_metrics(g, rng, shuffles=20))
        for m, s in methods.items():
            boot[m].append(scored_metrics({(k, n): s[k] for n, k in enumerate(sample)}, g))
    out = {}
    for m, p in point.items():
        out[m] = {"point": p, "ci": {}, "vs_bayes": {}}
        for metric in p:
            vals = sorted(b[metric] for b in boot[m])
            out[m]["ci"][metric] = [vals[int(0.025 * BOOTSTRAP)], vals[int(0.975 * BOOTSTRAP) - 1]]
            if "bayes" in boot and m != "bayes":
                diffs = sorted(b[metric] - c[metric] for b, c in zip(boot[m], boot["bayes"], strict=True))
                out[m]["vs_bayes"][metric] = [
                    p[metric] - point["bayes"][metric],
                    diffs[int(0.025 * BOOTSTRAP)],
                    diffs[int(0.975 * BOOTSTRAP) - 1],
                ]
    return out


# ----------------------------------------------------------------- the app's methods on a copy
def score_methods(db, products) -> dict[str, dict]:
    from app.services import preference_model as pm
    from app.services.recommender import rank_products, rules_component
    from app.services.settings_store import get_setting, set_setting

    taste = get_setting(db, "taste_profile")
    out: dict[str, dict] = {"profile": {p.id: rules_component(p, taste)[0] for p in products}}
    for engine in ("legacy", "bayes"):
        cfg = get_setting(db, "recommender")
        cfg["engine"] = engine
        set_setting(db, "recommender", cfg)
        pm._space_cache.clear()
        ranked = rank_products(db, products)
        out[engine] = {r.product.id: r.score for r in ranked}
        out[f"{engine} (taste only)"] = {r.product.id: r.design_score for r in ranked}
    return out


def run(db, test=None, seed: int = 0) -> dict:
    """Evaluate on the session's database (a copy: everything linked to a graded garment is deleted from it).
    ``test``: an eval/heldout.TestSet (a frozen benchmark's records); default, your current grades, unfrozen."""
    from app.models import Feedback, Product
    from app.services.preference_model import ENGINE_VERSION
    from app.services.provenance import Identity

    from .heldout import describe, purge_copy, unfrozen

    ident = Identity(db)
    test = test or unfrozen(db, ident)
    scored = [g for g in test.garments if g.product_id is not None]
    wanted = [g.product_id for g in scored]
    products_by_id = {p.id: p for p in db.query(Product).filter(Product.id.in_(wanted))} if wanted else {}
    sellers = {products_by_id[g.product_id].seller_id for g in scored}
    liked = sum(g.approved for g in scored)
    summary = describe(test) | {
        "sellers": len(sellers), "frozen": test.source["kind"] == "benchmark",
        "test_set_hash": hashlib.sha256("|".join(sorted(f"{g.family}:{g.taste}" for g in scored)).encode())
        .hexdigest()[:16],
    }  # fmt: skip
    if len(scored) < MIN_JUDGEMENTS or len(sellers) < MIN_SELLERS or liked < MIN_LIKED:
        return {"summary": summary, "ready": False}

    held = ident.resolve(**test.seeds)
    removed = purge_copy(db, held)  # the copy: nothing linked to a graded garment can reach any method
    summary |= {"training_decisions_removed": removed["decisions"], "training_removed": removed,
                "held_out": held.summary()}  # fmt: skip
    trained_sellers = {
        sid
        for (sid,) in db.query(Product.seller_id).join(Feedback, Feedback.product_id == Product.id)
        if sid is not None
    }
    products = [products_by_id[g.product_id] for g in scored]
    grades = {g.product_id: g.taste for g in scored}
    units = {g.product_id: g.unit for g in scored}
    methods = score_methods(db, products)
    result = {"summary": summary, "ready": True, "engine_version": ENGINE_VERSION,
              "all": evaluate_scores(methods, grades, seed, units)}  # fmt: skip
    new = {pid: g for pid, g in grades.items() if products_by_id[pid].seller_id not in trained_sellers}
    if len(new) >= 10 and sum(g >= 2 for g in new.values()) >= 2:
        result["new_sellers"] = evaluate_scores({m: {p: s[p] for p in new} for m, s in methods.items()}, new, seed,
                                                units)  # fmt: skip
    summary["new_seller_items"] = len(new)
    return result


# ----------------------------------------------------------------- report
def _fmt(v: float, ci: list[float]) -> str:
    return f"{v:.2f} [{ci[0]:.2f}, {ci[1]:.2f}]"


def report(result: dict) -> str:
    s = result["summary"]
    src = s.get("test_set") or {"kind": "unfrozen"}
    if src["kind"] == "benchmark":
        origin = (f"benchmark {src['benchmark']} (frozen {src['frozen_at'][:19]}, hash {src['hash'][:16]}); training: "
                  + ("its snapshot" if src["training"] == "snapshot" else
                     f"your database now{' before ' + src['cutoff'] if src.get('cutoff') else ''}"))  # fmt: skip
    else:
        origin = "your current grades, not frozen (freeze them before tuning: python -m eval.benchmark freeze)"
    lines = [
        f"Test set: {origin}",
        f"{s['garments']} graded garments ({s['judgements']} judgements) across {s['sellers']} sellers, "
        f"{s['liked']} liked · records hash {s['test_set_hash']}",
    ]  # fmt: skip
    if s.get("missing_products"):
        lines.append(f"{s['missing_products']} graded garments have no listing any more and were left out.")
    if s.get("merged"):
        lines.append(f"{s['merged']} garments were graded more than once (as separate listings later matched); "
                     "each counts once, with its first grade.")  # fmt: skip
    if not result["ready"]:
        lines.append(
            f"Not enough labels yet: at least {MIN_JUDGEMENTS} judgements, {MIN_SELLERS} sellers and {MIN_LIKED} "
            "liked pieces are needed before any comparison means something. Nothing was measured."
        )
        return "\n".join(lines)
    r = s.get("training_removed") or {"decisions": s["training_decisions_removed"]}
    lines.append("Removed from training as linked to graded garments: "
                 + ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in r.items()))  # fmt: skip
    if s.get("conflicts"):
        lines.append(f"Left out, grades disagree about one garment: {len(s['conflicts'])} "
                     f"(judgements {', '.join(str(c['judgement_ids']) for c in s['conflicts'])}).")  # fmt: skip
    for title, block in (
        ("All judged pieces", result["all"]),
        ("Sellers you never decided on", result.get("new_sellers")),
    ):
        if block is None:
            continue
        lines += ["", f"## {title}", "", "| Method | NDCG@10 | Precision@10 | Liked in top 20 | NDCG@10 vs bayes |",
                  "|---|---|---|---|---|"]  # fmt: skip
        for m, r in block.items():
            p, ci = r["point"], r["ci"]
            vs = r["vs_bayes"].get("ndcg@10")
            delta = f"{vs[0]:+.2f} [{vs[1]:+.2f}, {vs[2]:+.2f}]" if vs else "–"
            lines.append(
                f"| {m} | {_fmt(p['ndcg@10'], ci['ndcg@10'])} | {_fmt(p['p@10'], ci['p@10'])} | "
                f"{p['liked@20']:.1f} | {delta} |"
            )
    lines += ["", "Intervals: 95% bootstrap over judged families. A difference whose interval includes 0 is not "
              "demonstrated."]  # fmt: skip
    return "\n".join(lines)


# ----------------------------------------------------------------- command line
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", help="database file (default: this installation's)")
    ap.add_argument("--data-dir", help="where benchmarks and results are kept (default: WS_DATA_DIR)")
    ap.add_argument("--benchmark", help="a frozen benchmark version to measure on, e.g. pilot@v1 (eval/benchmark.py)")
    ap.add_argument("--training", choices=("snapshot", "live"), default="snapshot",
                    help="with --benchmark: train on its snapshot (default) or on your database now")
    ap.add_argument("--cutoff", help="with --training live: only what was known before this time (ISO 8601)")
    ap.add_argument("--freeze", action="store_true",
                    help=f"freeze the current grades as benchmark '{bm.DEFAULT_NAME}' (python -m eval.benchmark freeze)")
    ap.add_argument("--new-version", action="store_true", help="with --freeze: a successor of an existing version")
    ap.add_argument("--all", action="store_true", help="(kept for compatibility) your current grades, unfrozen")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args(argv)

    from app.config import get_settings

    from .dbcopy import open_copy, source_database

    src = source_database(args.db)
    if not src.is_file():
        print(f"database not found: {src}", file=sys.stderr)
        return 2
    out_dir = Path(args.data_dir) if args.data_dir else get_settings().data_dir
    eval_dir = out_dir / "evaluation"
    cutoff = datetime.fromisoformat(args.cutoff) if args.cutoff else None
    try:
        if args.freeze:
            m = bm.freeze(src, out_dir, bm.DEFAULT_NAME, new_version=args.new_version, seed=args.seed)
            c = m["counts"]
            print(f"Froze {c['records']} graded garments ({c['judgements']} judgements) as {m['name']}@v{m['version']} "
                  f"· hash {m['content_sha256'][:16]}. Measure on it with --benchmark {m['name']}@v{m['version']}.")  # fmt: skip
            return 0
        bench = bm.load(out_dir, args.benchmark) if args.benchmark else None
        with tempfile.TemporaryDirectory(prefix="ws-eval-") as tmp:
            if bench is not None:
                db = bm.open_training(bench, args.training, src, Path(tmp), cutoff)
            else:
                if cutoff is not None or args.training != "snapshot":
                    print("--training and --cutoff need --benchmark", file=sys.stderr)
                    return 2
                db = open_copy(src, Path(tmp))
            try:
                test = None
                if bench is not None:
                    test = bm.test_set(db, bench, args.training)
                    test.source["cutoff"] = cutoff.isoformat() if cutoff else None
                elif bm.legacy_path(out_dir).is_file():
                    from app.models import Judgement

                    st = bm.legacy_status(bm.legacy_path(out_dir), db.query(Judgement).all())
                    print(f"Note: an ID-only test set frozen {st['created_at'][:19]} ({st['ids']} ids) exists. It holds "
                          "no records and cannot be verified, so it is not used: "
                          f"{len(st['reused_after_freeze'])} of its ids now name grades made after it, "
                          f"{len(st['gone'])} are gone. See python -m eval.benchmark legacy.\n")  # fmt: skip
                entry_db = bm.run_entry(db, "eval.real", args.training if bench else "live", cutoff, None)
                result = run(db, test, args.seed)
            finally:
                db.close()
                bm._dispose_engine()
    except bm.BenchmarkError as e:
        print(str(e), file=sys.stderr)
        return 3

    print(report(result))
    path = None
    if result["ready"]:
        eval_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        path = eval_dir / f"results-{stamp}.json"
        path.write_text(json.dumps({"created_at": stamp, "run": entry_db, **result}, indent=1, default=str))
        print(f"\nSaved: {path}")
    if bench is not None:
        bm.append_run(bench, {**entry_db, "result": str(path) if path else None, "ready": result["ready"]})
    return 0


if __name__ == "__main__":
    sys.exit(main())
