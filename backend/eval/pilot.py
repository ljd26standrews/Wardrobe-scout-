"""A pilot on your own data: how well does selective recommendation work for you, and how sure can anyone be?

Usage (from backend/):
    .venv/bin/python -m eval.pilot plan                                  # what a pilot needs; what you have so far
    .venv/bin/python -m eval.pilot report --benchmark pilot@v1           # the report, on a frozen benchmark
    .venv/bin/python -m eval.pilot report --benchmark pilot@v1 --training live    # same garments, today's decisions

The pilot (docs/pilot/README.md):
1. A catalogue of PRODUCTS listings you can read (a title, a price, an analysed photo).
2. GRADED garments graded on the Evaluate page: drawn uniformly from undecided listings, graded for the look alone.
   They are held out: nothing linked to them trains anything (app/services/provenance.py).
3. Training decisions on Discover, made separately, including Learning picks (exploration), which reach pieces the
   ranking alone would not show you.
4. The grades frozen as a benchmark before looking at any result (python -m eval.benchmark freeze --name pilot);
   then this report.

Nothing here invents or infers a grade: it uses your grades and decisions as stored and refuses a number when the
sample is too small to support it. With few grades even a perfect result is weak evidence: if every one of n selected
pieces was approved, the 95% Clopper-Pearson lower bound is (0.025)^(1/n): 88.4% at 30, 94.0% at 60, and it first
reaches 95% at 72. Accuracy for you is never "proven" by this report; it is measured, with intervals.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics as st
import sys
import tempfile
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

PRODUCTS = (150, 300)
GRADED = (40, 60)
TRAINING_MIN = 60  # rated decisions on Discover (the calibration itself needs 30 by default)
EXPLORATION_MIN = 10  # of them on Learning picks
CP_SIZES = (10, 20, 30, 40, 50, 60, 72, 100)


# ----------------------------------------------------------------- what you have
def _check(n: int, lo: int, hi: int | None = None) -> dict:
    if n < lo:
        return {"have": n, "status": "short", "need": lo - n, "range": [lo, hi]}
    if hi is not None and n > hi:
        return {"have": n, "status": "over", "range": [lo, hi]}
    return {"have": n, "status": "ok", "range": [lo, hi]}


def plan(db) -> dict:
    """The pilot's requirements against what this database holds."""
    from app.models import Impression, Product
    from app.services import embeddings as emb
    from app.services import exposure
    from app.services.approval_data import rated_outcomes
    from app.services.provenance import Identity

    from .heldout import unfrozen

    readable = (db.query(Product).filter(Product.status == "active", Product.price.isnot(None),
                                         Product.image_embedding.isnot(None)).count())  # fmt: skip
    test = unfrozen(db, Identity(db))
    graded = [g for g in test.garments if g.product_id is not None]
    outcomes = rated_outcomes(db)
    explore = [o for o in outcomes if o.explore or o.surface == "learning"]
    backends = Counter(b for (b,) in db.query(Product.embedding_backend) if b)
    exposures = db.query(Impression).filter(Impression.definition == exposure.DEFINITION).count()
    return {
        "products": _check(readable, *PRODUCTS),
        "graded": _check(len(graded), *GRADED) | {"approved": sum(g.approved for g in graded),
                                                  "conflicts": len(test.report["conflicts"])},
        "training_decisions": _check(len(outcomes), TRAINING_MIN) | {"approvals": sum(o.y for o in outcomes)},
        "exploration_decisions": _check(len(explore), EXPLORATION_MIN),
        "exposures": {"have": exposures, "definition": exposure.DEFINITION,
                      "legacy": db.query(Impression).filter(Impression.definition.is_(None)).count()},
        "image_backend": {"now": emb.get_image_backend().name, "listings_by_backend": dict(backends)},
        "calibration": _calibration_state(db),
    }  # fmt: skip


def _calibration_state(db) -> str:
    """The calibration state every surface reports, without queueing a refit (this runs on a copy)."""
    from app.services import calibration
    from app.services.settings_store import get_setting

    cur, ev = calibration.serving(db)
    if cur is None:
        return calibration.waiting_state(db, ev)
    return "stale" if calibration.drift(cur, ev, get_setting(db, "selective"))["refit_due"] else "calibrated"


def backend_statement(name: str, by_backend: dict) -> str:
    total = sum(by_backend.values())
    counts = ", ".join(f"{k}: {v}" for k, v in sorted(by_backend.items())) or "none analysed"
    if name.startswith("clip"):
        return f"Photos are analysed with CLIP ({name}); listings by method: {counts}."
    return (f"Photos are analysed with the lightweight colour-and-shape features ({name}), not CLIP; listings by "
            f"method: {counts}. Nothing in this report says how CLIP would do, better or worse."
            + ("" if total else " No listing has an analysed photo yet."))  # fmt: skip


# ----------------------------------------------------------------- what Discover served and you saw
def logged_coverage(db) -> dict:
    """Per mode, the share of eligible pieces the gate let through, over the selective feeds you were served."""
    from app.models import FeedRequest

    shares: dict[str, list[float]] = {}
    for f in db.query(FeedRequest).filter(FeedRequest.policy == "selective"):
        g = f.gate or {}
        if g.get("qualified") is not None and g.get("eligible"):
            shares.setdefault(f.mode or "?", []).append(g["qualified"] / g["eligible"])
    out = {}
    for mode, xs in sorted(shares.items()):
        q = st.quantiles(xs, n=4) if len(xs) >= 2 else [xs[0]] * 3
        out[mode] = {"feeds": len(xs), "median": round(st.median(xs), 4), "iqr": [round(q[0], 4), round(q[2], 4)]}
    return out


def variety(db) -> dict:
    """What you saw (exposures under the current definition): how many garments, how many kinds, how evenly."""
    from app.models import Impression, Product
    from app.services import exposure

    rows = (db.query(Impression.product_id, Product.category, Product.seller_id, Product.group_id)
            .join(Product, Product.id == Impression.product_id)
            .filter(Impression.definition == exposure.DEFINITION).all())  # fmt: skip
    if not rows:
        return {"unavailable": f"no exposure recorded under {exposure.DEFINITION} yet"}
    garments = {}
    for pid, cat, seller, gid in rows:
        garments.setdefault(f"g{gid}" if gid is not None else f"p{pid}", (cat or "unknown", seller))
    cats = Counter(c for c, _ in garments.values())
    n = sum(cats.values())
    entropy = -sum(k / n * math.log(k / n) for k in cats.values())
    return {"garments_seen": n, "categories": len(cats), "effective_categories": round(math.exp(entropy), 2),
            "top_category_share": round(cats.most_common(1)[0][1] / n, 4),
            "sellers": len({s for _, s in garments.values() if s is not None})}  # fmt: skip


def clopper_pearson_table() -> list[dict]:
    from .selective import clopper_pearson

    return [{"selected": n, "all_approved_lower_bound": round(clopper_pearson(n, n)[0], 4)} for n in CP_SIZES]


def taste_and_quality(bench) -> dict:
    """Grades are about the look alone; the flags given with them (quality unclear, too expensive, owning something
    similar) are counted here and are part of no taste metric."""
    flags = Counter()
    for r in bench.records:
        for k, v in (r.get("flags") or {}).items():
            flags[k] += bool(v)
    return {"graded_garments": len(bench.records), "flags": dict(flags)}


# ----------------------------------------------------------------- the report
def build(db, bench, training: str, cutoff: datetime | None) -> dict:
    from . import benchmark as bm
    from .selective import run_all

    test = bm.test_set(db, bench, training)
    test.source["cutoff"] = cutoff.isoformat() if cutoff else None
    before = {"plan": plan(db), "logged_coverage": logged_coverage(db), "variety": variety(db)}
    run = bm.run_entry(db, "eval.pilot", training, cutoff, None)
    result = run_all(db, test=test)  # the representative protocol runs last and deletes held-out records (a copy)
    return {"benchmark": {"ref": bench.ref, "hash": bench.hash, "frozen_at": bench.manifest["created_at"],
                          "training": training, "cutoff": test.source["cutoff"]},
            "run": run, **before, "clopper_pearson": clopper_pearson_table(),
            "taste_and_quality": taste_and_quality(bench),
            "image_backend": backend_statement(run["image_backend"], run["data"]["photo_analysis"]),
            "selective": result}  # fmt: skip


def _pct(v) -> str:
    return "–" if v is None else f"{100 * v:.1f}%"


def _prec(r: dict) -> str:
    if r.get("precision") is None:
        return f"{r['approved']}/{r['selected']} (too few to give a rate)"
    lo, hi = r["interval"]
    return f"{_pct(r['precision'])} [{_pct(lo)}, {_pct(hi)}] of {r['selected']}"


def markdown(rep: dict) -> str:
    b, run, p, sel = rep["benchmark"], rep["run"], rep["plan"], rep["selective"]
    lines = ["# Pilot report", "",
             f"Benchmark **{b['ref']}** (hash `{b['hash'][:16]}`, frozen {b['frozen_at'][:19]}); training data: "
             + ("its snapshot." if b["training"] == "snapshot"
                else f"your database at the time of this run{', before ' + b['cutoff'] if b['cutoff'] else ''}."),
             f"Engine `{run['engine']}`, labels `{run['labels']}`, calibration `{run['calibration_method']}`, code "
             f"`{run['code'].get('git_commit', run['code']['sources_sha256'])[:12]}`.", "", rep["image_backend"], "",
             "## The pilot's requirements", "", "| | Have | Wanted | |", "|---|---|---|---|"]  # fmt: skip
    for key, label in (("products", "Readable listings"), ("graded", "Graded garments (held out)"),
                       ("training_decisions", "Rated decisions on Discover"),
                       ("exploration_decisions", "… of them on Learning picks")):  # fmt: skip
        c = p[key]
        lo, hi = c["range"]
        want = f"{lo}–{hi}" if hi else f"≥ {lo}"
        note = {"ok": "ok", "over": "more than planned (fine)", "short": f"{c.get('need')} short"}[c["status"]]
        lines.append(f"| {label} | {c['have']} | {want} | {note} |")
    lines += ["", "## How much a result can show", "",
              "If every selected piece was approved, the 95% Clopper-Pearson lower bound is:", "",
              "| Selected | Lower bound |", "|---|---|"]  # fmt: skip
    lines += [f"| {r['selected']} | {_pct(r['all_approved_lower_bound'])} |" for r in rep["clopper_pearson"]]
    lines += ["", "A mode meets a 95% target only when its lower bound clears 95%, which needs at least 72 selected "
              "pieces, all approved. Accuracy for you is measured here, never proven.", ""]  # fmt: skip
    r = sel.get("representative") or {}
    s = r.get("summary") or {}
    lines += ["## Your graded garments", ""]
    if "refused" in s or not r.get("predictions"):
        lines += [f"Not measured: {s.get('refused', 'no predictions')}.", ""]
    else:
        lines += [f"{s['garments']} graded garments ({s['liked']} approved); everything linked to them was left out of "
                  f"training ({', '.join(f'{k.replace(chr(95), chr(32))}: {v}' for k, v in s['training_removed'].items())}).",
                  "", "| Mode | Coverage | Gate | Written profile | Learned taste | Full Discover score |",
                  "|---|---|---|---|---|---|"]  # fmt: skip
        if "refused" in r["modes"]:
            lines.append(f"| – | – | refused: {r['modes']['refused']} | | | |")
        for mode in ("balanced", "high", "extreme"):
            g = r["modes"].get(mode)
            if not g or "refused" in r["modes"]:
                continue
            lines.append(f"| {mode} ({_pct(g['target'])} at {_pct(g['confidence'])}) | {_pct(g['coverage'])} | "
                         f"{_prec(g['gate'])} | {_prec(g.get('written_profile_same_coverage', {'approved': 0, 'selected': 0}))} | "
                         f"{_prec(g['taste_ranking_same_coverage'])} | "
                         f"{_prec(g.get('discover_score_same_coverage', {'approved': 0, 'selected': 0}))} |")  # fmt: skip
        if "all_rated" in r["modes"]:
            lines += ["", "Each comparison takes as many pieces as the gate did (the same coverage); ties are counted "
                      "in expectation. Showing every graded piece instead: " + _prec(r["modes"]["all_rated"]) + "."]  # fmt: skip
        lines.append("")
        cal = r.get("calibrated") or {}
        raw = r.get("raw") or {}
        lines += ["Calibration on the held-out grades (never used to fit anything):", "",
                  "| Estimate | n | Brier | ECE | AUC |", "|---|---|---|---|---|"]  # fmt: skip
        for name, c in (("raw σ(m)", raw), ("calibrated", cal)):
            if "refused" in c:
                lines.append(f"| {name} | {c.get('n')} | refused: {c['refused']} | | |")
            elif c:
                lines.append(f"| {name} | {c['n']} | {c['brier']:.3f} | {_pct(c['ece'])} | {c['auc']} |")
        lines += ["", "### By garment type", "", "| Type | Graded | Approved | Balanced gate |", "|---|---|---|---|"]
        for cat, row in (r.get("categories") or {}).items():
            lines.append(f"| {cat} | {row['predictions']} | {row['approvals']} | {_prec(row['balanced'])} |")
        u = r.get("unfamiliar_sellers") or {}
        lines += ["", f"Sellers you had never rated before: {u.get('predictions', 0)} graded garments "
                  f"({u.get('approvals', 0)} approved)."]  # fmt: skip
        if "balanced" in (u.get("modes") or {}):
            lines.append(f"Balanced gate on them: {_prec(u['modes']['balanced']['gate'])}.")
        th = r.get("suggested_thresholds") or {}
        if th:
            lines += ["", "Thresholds chosen on one half of the graded garments, measured on the other:", ""]
            for mode, t in th.items():
                lines.append(f"- {mode}: " + (t["refused"] if "refused" in t else f"bound ≥ {t['threshold']}: {_prec(t)}"))
        lines.append("")
    lg = sel.get("logged") or {}
    lines += ["## Your Discover decisions, predicted in time order", ""]
    if "refused" in lg:
        lines += [f"Not measured: {lg['refused']}.", ""]
    else:
        c = lg.get("calibrated") or {}
        lines += [f"{lg['predictions']} decisions, each predicted by a model fitted only on earlier ones: "
                  + (f"refused ({c['refused']})." if "refused" in c else
                     f"Brier {c['brier']:.3f}, ECE {_pct(c['ece'])}, AUC {c['auc']}."), ""]  # fmt: skip
    sv = sel.get("served") or {}
    lines += ["## What Discover served and you saw", "",
              f"Exposure: `{sv.get('exposure_definition')}` (on screen long enough, or decided about while on screen)."
              + (f" {sv['legacy_records']} earlier records counted rendered tiles and are not used." if sv.get("legacy_records") else ""),
              ""]  # fmt: skip
    if not sv.get("empty_feeds"):
        lines.append("- No Discover feed has been logged yet.")
    elif not rep["logged_coverage"]:
        lines.append("- No selective feed with approval estimates has been logged yet: gate coverage unavailable.")
    for k, e in (sv.get("empty_feeds") or {}).items():
        lines.append(f"- Empty feed answers, {k}: {e['count']} of {e['of']}" + (f" ({_pct(e['rate'])})" if e["rate"] is not None else ""))
    for mode, c in rep["logged_coverage"].items():
        lines.append(f"- Gate coverage, {mode}: median {_pct(c['median'])} of eligible pieces over {c['feeds']} feeds "
                     f"(middle half {_pct(c['iqr'][0])}–{_pct(c['iqr'][1])})")  # fmt: skip
    for name, key in (("Approved after first seeing it", "discoveries_per_exposure"), ("Seen again in a later feed", "repetition")):
        d = sv.get(key) or {}
        if "unavailable" in d:
            lines.append(f"- {name}: unavailable ({d['unavailable']})")
        else:
            row = d.get("all", d)
            lines.append(f"- {name}: {row['count']} of {row['of']}" + (f" ({_pct(row['rate'])})" if row["rate"] is not None else " (too few for a rate)"))
    v = rep["variety"]
    lines.append("- Variety of what you saw: " + (f"unavailable ({v['unavailable']})" if "unavailable" in v else
                 f"{v['garments_seen']} garments, {v['categories']} garment types (about {v['effective_categories']} "
                 f"evenly used), the most common {_pct(v['top_category_share'])}; {v['sellers']} sellers"))  # fmt: skip
    tq = rep["taste_and_quality"]
    lines += ["", "## Taste and quality", "",
              f"Grades are about the look alone. Flags given with the {tq['graded_garments']} graded garments: "
              + (", ".join(f"{k.replace('_', ' ')} {v}" for k, v in tq["flags"].items()) or "none")
              + ". They are part of no taste metric; the full Discover score (which includes quality and value) is "
              "reported separately from the taste ranking.", "",
              "## What this does not show", "",
              "- It measures the graded garments: listings drawn uniformly from undecided ones in your catalogue. It "
              "says nothing about pieces unlike them, or about another person.",
              "- With fewer pieces than the table above needs, a target is not met, however good the point estimate.",
              "- Learning picks were chosen to explore, not to please: they are reported apart where it matters."]  # fmt: skip
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", help="database file (default: this installation's)")
    ap.add_argument("--data-dir", help="where benchmarks and reports are kept (default: WS_DATA_DIR)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("plan", help="what a pilot needs, and what you have so far")
    r = sub.add_parser("report", help="the pilot report on a frozen benchmark")
    r.add_argument("--benchmark", required=True, help="e.g. pilot@v1 (python -m eval.benchmark freeze --name pilot)")
    r.add_argument("--training", choices=("snapshot", "live"), default="snapshot")
    r.add_argument("--cutoff", help="with --training live: only what was known before this time (ISO 8601)")
    args = ap.parse_args(argv)

    from app.config import get_settings

    from . import benchmark as bm
    from .dbcopy import close_copy, open_copy, source_database

    src = source_database(args.db)
    if not src.is_file():
        print(f"database not found: {src}", file=sys.stderr)
        return 2
    data_dir = Path(args.data_dir) if args.data_dir else get_settings().data_dir
    try:
        with tempfile.TemporaryDirectory(prefix="ws-pilot-") as tmp:
            if args.cmd == "plan":
                db = open_copy(src, Path(tmp))
                try:
                    print(json.dumps(plan(db), indent=1))
                finally:
                    close_copy(db)
                return 0
            cutoff = datetime.fromisoformat(args.cutoff) if args.cutoff else None
            bench = bm.load(data_dir, args.benchmark)
            db = bm.open_training(bench, args.training, src, Path(tmp), cutoff)
            try:
                rep = build(db, bench, args.training, cutoff)
            finally:
                close_copy(db)
    except bm.BenchmarkError as e:
        print(str(e), file=sys.stderr)
        return 3
    text = markdown(rep)
    print(text)
    out = data_dir / "evaluation" / "pilot"
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    (out / f"{bench.name}-v{bench.version}-{stamp}.md").write_text(text, encoding="utf-8")
    path = out / f"{bench.name}-v{bench.version}-{stamp}.json"
    path.write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
    bm.append_run(bench, {**rep["run"], "result": str(path)})
    print(f"Saved: {path.with_suffix('.md')} and {path.name}")
    return 0


if __name__ == "__main__":
    from .console import configure_console

    configure_console()
    sys.exit(main())
