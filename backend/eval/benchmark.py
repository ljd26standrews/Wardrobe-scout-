"""Frozen, versioned benchmarks: the graded garments both evaluators measure on, kept as data.

Usage (from backend/):
    .venv/bin/python -m eval.benchmark freeze --name pilot                # pilot@v1 (refused if it exists)
    .venv/bin/python -m eval.benchmark freeze --name pilot --new-version  # pilot@v2, a deliberate successor
    .venv/bin/python -m eval.benchmark list                               # versions, their hashes, any problem
    .venv/bin/python -m eval.benchmark verify pilot@v1                    # recompute the hashes
    .venv/bin/python -m eval.benchmark legacy                             # what an old ID-only freeze holds
    .venv/bin/python -m eval.benchmark freeze --name pilot --from-legacy  # rebuild it as a benchmark, honestly
    .venv/bin/python -m eval.real --benchmark pilot@v1 [--training live [--cutoff ISO]]
    .venv/bin/python -m eval.selective --benchmark pilot@v1 [--training live [--cutoff ISO]]

A version is a directory, written once and then made read-only:

    <data dir>/evaluation/benchmarks/<name>/v<N>/
        manifest.json   format, creation time, training cutoff, protocol, counts, identifiers of the data, written
                        profile, photo representation, engine, label reading and code, the hashes below, the parent
        records.jsonl   one line per graded garment: the scored listing's stable identity (seller, the seller's id
                        for it, address), the family's listings, the grades and flags, the split half, the snapshot
                        of the piece as graded and a fingerprint of its content
        snapshot.db     the database as it was when frozen (SQLite online backup, migrated): the population and the
                        training data, retained, not just fingerprinted
    <data dir>/evaluation/benchmarks/<name>/runs.jsonl   one line per evaluation run, appended

Evaluating a version reads its records, never the judgements table, so an Undo, a re-grade, a reused judgement id,
an import or a changed listing cannot substitute a record. Training comes from
* ``snapshot`` (default): the copy frozen with it. Reproducible: the same records, the same training data;
* ``live``: a copy of your database now (optionally only what was known before ``--cutoff``): the frozen population
  measured with today's training data. Each record must still resolve, by its stable identity, to exactly one
  listing, and no two records may have become one garment (or share a split unit) since: otherwise the run is
  refused and a successor version is the way forward. Listings whose content changed are reported.

The benchmark's hash covers the records, the snapshot and the protocol; every report names the version and hash.
An earlier, ID-only freeze (evaluation/frozen_test_set.json) holds no records and cannot be verified: it is never
used as a benchmark. ``--from-legacy`` rebuilds one from it, keeping only judgements that existed when it was frozen
(an id created later is a reused id) and saying so in the manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

FORMAT = "wardrobe-scout-benchmark/1"
POLICY = "heldout-v1"  # eval/heldout.py and services/provenance.py
SPLIT = "sha256(seed:unit) % 2"
DEFAULT_NAME = "default"
CODE_FILES = ("eval/benchmark.py", "eval/heldout.py", "eval/real.py", "eval/selective.py",
              "app/services/provenance.py", "app/services/preference_model.py", "app/services/calibration.py",
              "app/services/approval_data.py", "app/services/selective.py", "app/services/recommender.py")  # fmt: skip
BACKEND = Path(__file__).resolve().parents[1]


class BenchmarkError(Exception):
    """A benchmark that may not be created or used as asked (the message says why and what to do)."""


# ----------------------------------------------------------------- locations and hashes
def root(data_dir: Path) -> Path:
    return Path(data_dir) / "evaluation" / "benchmarks"


def versions(data_dir: Path, name: str) -> list[int]:
    d = root(data_dir) / name
    if not d.is_dir():
        return []
    return sorted(int(p.name[1:]) for p in d.iterdir() if p.is_dir() and p.name[:1] == "v" and p.name[1:].isdigit())


def parse_ref(ref: str) -> tuple[str, int | None]:
    """"name@v3" -> ("name", 3); "name" -> ("name", None: the latest)."""
    name, _, v = ref.partition("@")
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise BenchmarkError(f"not a benchmark name: {ref!r}")
    if not v:
        return name, None
    if not (v[:1] == "v" and v[1:].isdigit()):
        raise BenchmarkError(f"not a version: {v!r} (expected v1, v2, ...)")
    return name, int(v[1:])


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def content_hash(manifest: dict) -> str:
    """The benchmark's identity: its records, its snapshot and its protocol."""
    body = {"records": manifest["records_sha256"], "snapshot": manifest["snapshot"]["sha256"],
            "protocol": manifest["protocol"]}  # fmt: skip
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


def code_identity() -> dict:
    """The code that froze or ran it: the commit when there is one, and a hash of the evaluation sources."""
    h = hashlib.sha256()
    for rel in CODE_FILES:
        p = BACKEND / rel
        h.update(rel.encode() + b"\0" + (p.read_bytes() if p.is_file() else b"") + b"\0")
    out: dict = {"sources_sha256": h.hexdigest()}
    try:
        rev = subprocess.run(["git", "rev-parse", "HEAD"], cwd=BACKEND, capture_output=True, text=True, timeout=10)
        if rev.returncode == 0:
            out["git_commit"] = rev.stdout.strip()
            dirty = subprocess.run(["git", "status", "--porcelain", "--", "."], cwd=BACKEND, capture_output=True,
                                   text=True, timeout=10)  # fmt: skip
            out["git_dirty"] = bool(dirty.stdout.strip()) if dirty.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        pass
    return out


# ----------------------------------------------------------------- what a record holds
def _listing(p) -> dict:
    return {"product_id": p.id, "seller_id": p.seller_id, "external_id": p.external_id, "source_url": p.source_url}


def content_fingerprint(p) -> str:
    """What a listing's prediction depends on (its content, photo vector and representation)."""
    body = [p.title_original, p.title_en, p.brand, p.category, p.colours, p.materials, p.fit_tags, p.logo_level,
            p.price, p.currency, p.embedding_backend,
            hashlib.sha256(p.image_embedding).hexdigest() if p.image_embedding else None]  # fmt: skip
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()[:16]


def split_half(seed: int, unit: str) -> int:
    return int(hashlib.sha256(f"{seed}:{unit}".encode()).hexdigest(), 16) % 2


def build_records(db, judgements, *, seed: int = 0) -> tuple[list[dict], dict, dict]:
    """(records, grouping report, held-out seeds) for ``judgements`` in ``db``."""
    from app.models import Product
    from app.services.provenance import Identity

    from .heldout import graded_garments

    ident = Identity(db)
    garments, report = graded_garments(db, judgements, ident)
    by_id = {j.id: j for j in judgements}
    pids = {g.product_id for g in garments if g.product_id is not None}
    products = {p.id: p for p in db.query(Product).filter(Product.id.in_(pids))} if pids else {}
    records = []
    for n, g in enumerate(garments, 1):
        p = products.get(g.product_id)
        family = sorted(ident.closure([g.product_id])[0]) if p is not None else []
        definite = [q for q in family if ident.family(q) == g.family or ident.url_of.get(q) == ident.url_of.get(p.id)]
        members = {q.id: q for q in db.query(Product).filter(Product.id.in_(definite))} if definite else {}
        first = by_id[g.judgement_ids[0]]
        records.append({
            "record": n, "family": g.family, "unit": g.unit, "split": split_half(seed, g.unit),
            "taste": g.taste, "approved": g.approved, "grades": g.grades, "judgement_ids": g.judgement_ids,
            "judged_at": g.created_at.isoformat(),
            "flags": {"owned_similar": bool(first.owned_similar), "too_expensive": bool(first.too_expensive),
                      "quality_unclear": bool(first.quality_unclear)},
            "sampled_by": first.sampled_by,
            "listing": _listing(p) if p is not None else None,
            "members": [_listing(members[q]) for q in sorted(members)],
            "content": content_fingerprint(p) if p is not None else None,
            "snapshot": g.snapshot,
        })  # fmt: skip
    products_all = {j.product_id for j in judgements if j.product_id is not None}
    live = {p.id: p for p in db.query(Product).filter(Product.id.in_(products_all))} if products_all else {}
    seeds = {
        "families": sorted({j.family_key for j in judgements}),
        "listings": sorted([[p.seller_id, p.external_id] for p in live.values()], key=str),
        "product_ids": sorted(live),
        "urls": sorted({u for j in judgements if (u := (j.snapshot or {}).get("source_url"))}),
    }
    return records, report, seeds


# ----------------------------------------------------------------- freezing
def _data_identity(db) -> dict:
    from sqlalchemy import func, text

    from app.models import Feedback, InspirationImage, Judgement, Product, WardrobeItem

    revs = {name: n for name, n in db.execute(text("SELECT name, n FROM data_revisions"))}
    try:
        alembic = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except Exception:  # noqa: BLE001 - created without migrations
        alembic = None
    backends = dict(db.query(Product.embedding_backend, func.count(Product.id)).group_by(Product.embedding_backend))
    latest = db.query(func.max(Feedback.created_at)).scalar()
    return {
        "database_instance": revs.pop("instance", None), "revisions": revs, "schema": alembic,
        "counts": {"products": db.query(Product).count(), "decisions": db.query(Feedback).count(),
                   "judgements": db.query(Judgement).count(), "wardrobe": db.query(WardrobeItem).count(),
                   "inspiration": db.query(InspirationImage).count()},
        "photo_analysis": {str(k): v for k, v in backends.items()},
        "latest_decision_at": latest.isoformat() if latest else None,
    }  # fmt: skip


def freeze(src: Path, data_dir: Path, name: str = DEFAULT_NAME, *, new_version: bool = False, seed: int = 0,
           legacy: Path | None = None) -> dict:  # fmt: skip
    """Freeze the current graded garments of the database at ``src`` as ``name@v<N>``. Never overwrites: a name that
    has versions needs ``new_version`` (a successor, recording its parent). Returns the manifest."""
    from app.models import Judgement
    from app.services import calibration
    from app.services import embeddings as emb
    from app.services import preference_model as pm
    from app.services.approval_data import LABEL_VERSION

    from .dbcopy import close_copy, open_copy

    parse_ref(name)
    have = versions(data_dir, name)
    if have and not new_version:
        raise BenchmarkError(f"{name}@v{have[-1]} already exists and a frozen benchmark is never overwritten. "
                             f"To freeze the current grades as a successor ({name}@v{have[-1] + 1}), add --new-version.")
    version = (have[-1] if have else 0) + 1
    base = root(data_dir) / name
    base.mkdir(parents=True, exist_ok=True)
    final = base / f"v{version}"
    stage = Path(tempfile.mkdtemp(prefix=f".v{version}-", dir=base))
    try:
        db = open_copy(src, stage)
        try:
            judgements = db.query(Judgement).order_by(Judgement.id).all()
            legacy_info = None
            if legacy is not None:
                judgements, legacy_info = _from_legacy(legacy, judgements)
            records, report, seeds = build_records(db, judgements, seed=seed)
            regime = calibration.regime(db)
            data = _data_identity(db)
        finally:
            close_copy(db)
        snap = stage / "snapshot.db"
        _single_file(stage / "eval.db", snap)
        lines = "".join(json.dumps(r, sort_keys=True, default=str) + "\n" for r in records)
        (stage / "records.jsonl").write_text(lines, encoding="utf-8")
        created = datetime.now(UTC).isoformat()
        sellers = {(r["listing"] or {}).get("seller_id") for r in records} - {None}
        categories = {(r["snapshot"] or {}).get("category") for r in records} - {None}
        manifest = {
            "format": FORMAT, "name": name, "version": version, "parent": f"{name}@v{version - 1}" if have else None,
            "created_at": created,
            "training_cutoff": created,  # the snapshot holds everything known when it was frozen
            "protocol": {"test_set": "Evaluate grades: pieces drawn uniformly from readable undecided garments, "
                                     "graded for taste alone (0-3)", "approval": "grade >= 2",
                         "unit": "one record per garment; agreeing grades merged (earliest), disagreeing left out",
                         "held_out": POLICY, "split": SPLIT, "split_seed": seed},
            "counts": {"judgements": len(judgements), "records": len(records),
                       "approved": sum(r["approved"] for r in records), "merged": len(report["merged"]),
                       "conflicts": len(report["conflicts"]), "missing_listing": len(report["missing"]),
                       "sellers": len(sellers), "categories": len(categories)},
            "excluded": {"conflicts": report["conflicts"], "missing_listing": report["missing"]},
            "held_out_seeds": seeds,
            "records_sha256": hashlib.sha256(lines.encode()).hexdigest(),
            "snapshot": {"file": "snapshot.db", "sha256": _sha256_file(snap), "bytes": snap.stat().st_size},
            "data": data, "profile": regime["profile"], "representation": regime["representation"],
            "engine": pm.ENGINE_VERSION, "labels": LABEL_VERSION, "calibration_method": calibration.METHOD,
            "image_backend_now": emb.get_image_backend().name, "code": code_identity(), "legacy": legacy_info,
        }  # fmt: skip
        manifest["content_sha256"] = content_hash(manifest)
        (stage / "manifest.json").write_text(json.dumps(manifest, indent=1, default=str), encoding="utf-8")
        for f in ("manifest.json", "records.jsonl", "snapshot.db"):
            os.chmod(stage / f, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
        if final.exists():
            raise BenchmarkError(f"{name}@v{version} appeared while freezing; nothing was overwritten")
        os.replace(stage, final)
        return manifest
    finally:
        if stage.exists():
            _rmtree(stage)


def _single_file(src: Path, dst: Path) -> None:
    """Copy a database (WAL folded in) to one self-contained file."""
    with sqlite3.connect(src) as a, sqlite3.connect(dst) as b:
        a.backup(b)
    with sqlite3.connect(dst) as b:
        b.execute("PRAGMA journal_mode=DELETE")
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(src) + suffix)
        if p.exists():
            p.unlink()


def _rmtree(path: Path) -> None:
    def onerror(func, p, _):
        os.chmod(p, stat.S_IWRITE)
        func(p)

    shutil.rmtree(path, onerror=onerror)  # read-only files (Windows) are made writable first


# ----------------------------------------------------------------- the legacy ID-only freeze
def legacy_path(data_dir: Path) -> Path:
    return Path(data_dir) / "evaluation" / "frozen_test_set.json"


def legacy_status(path: Path, judgements) -> dict:
    """What an ID-only freeze can and cannot tell: which of its ids still name a judgement made by then, which
    are gone, and which now name a judgement created after the freeze (a reused id: a different grade)."""
    data = json.loads(path.read_text())
    at = datetime.fromisoformat(data["created_at"]).replace(tzinfo=None)
    ids = set(data.get("judgement_ids") or [])
    have = {j.id: j for j in judgements}
    reused = sorted(i for i in ids if i in have and have[i].created_at > at)
    return {"file": str(path), "created_at": data["created_at"], "ids": len(ids),
            "still_there": len([i for i in ids if i in have and have[i].created_at <= at]),
            "gone": sorted(i for i in ids if i not in have), "reused_after_freeze": reused,
            "judged_after_freeze": len([j for j in judgements if j.created_at > at and j.id not in ids])}  # fmt: skip


def _from_legacy(path: Path, judgements) -> tuple[list, dict]:
    status = legacy_status(path, judgements)
    data = json.loads(path.read_text())
    at = datetime.fromisoformat(data["created_at"]).replace(tzinfo=None)
    ids = set(data.get("judgement_ids") or [])
    kept = [j for j in judgements if j.id in ids and j.created_at <= at]
    status["note"] = ("Rebuilt from an ID-only freeze: the records are the judgements those ids name now, kept only "
                      "if made before the freeze (a later one under a listed id is a reused id). Grades undone since "
                      "are gone and could not be recovered. The training snapshot is from the rebuild, not from the "
                      "original freeze.")  # fmt: skip
    return kept, status


# ----------------------------------------------------------------- loading and verifying
@dataclass
class Benchmark:
    name: str
    version: int
    path: Path
    manifest: dict
    records: list[dict]

    @property
    def ref(self) -> str:
        return f"{self.name}@v{self.version}"

    @property
    def hash(self) -> str:
        return self.manifest["content_sha256"]

    @property
    def snapshot(self) -> Path:
        return self.path / self.manifest["snapshot"]["file"]


def load(data_dir: Path, ref: str, *, verify_snapshot: bool = True) -> Benchmark:
    """A benchmark version, after checking its files still hash to what was frozen."""
    name, version = parse_ref(ref)
    have = versions(data_dir, name)
    if not have:
        legacy = " (an ID-only freeze exists: see `python -m eval.benchmark legacy`)" if legacy_path(data_dir).is_file() else ""
        raise BenchmarkError(f"no benchmark named {name!r} in {root(data_dir)}{legacy}")
    version = version or have[-1]
    if version not in have:
        raise BenchmarkError(f"{name}@v{version} does not exist (versions: {', '.join(f'v{v}' for v in have)})")
    path = root(data_dir) / name / f"v{version}"
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != FORMAT:
        raise BenchmarkError(f"{name}@v{version}: unknown format {manifest.get('format')!r}")
    raw = (path / "records.jsonl").read_bytes()
    problems = []
    if hashlib.sha256(raw).hexdigest() != manifest["records_sha256"]:
        problems.append("records.jsonl")
    if verify_snapshot and _sha256_file(path / manifest["snapshot"]["file"]) != manifest["snapshot"]["sha256"]:
        problems.append(manifest["snapshot"]["file"])
    if content_hash(manifest) != manifest.get("content_sha256"):
        problems.append("manifest.json")
    if problems:
        raise BenchmarkError(f"{name}@v{version} was changed after it was frozen ({', '.join(problems)} no longer "
                             "match their hashes): it cannot be used. Freeze a new version instead.")  # fmt: skip
    records = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
    return Benchmark(name, version, path, manifest, records)


def append_run(bench: Benchmark, entry: dict) -> None:
    """Record one evaluation run on ``bench`` (append-only), so reuse of a test set stays visible."""
    line = json.dumps({"at": datetime.now(UTC).isoformat(), "benchmark": bench.ref, "hash": bench.hash, **entry},
                      sort_keys=True, default=str)  # fmt: skip
    with open(bench.path.parent / "runs.jsonl", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def runs(bench: Benchmark) -> list[dict]:
    p = bench.path.parent / "runs.jsonl"
    if not p.is_file():
        return []
    return [r for r in (json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip())
            if r.get("benchmark") == bench.ref]  # fmt: skip


# ----------------------------------------------------------------- the test set in an evaluation database
def test_set(db, bench: Benchmark, training: str):
    """The benchmark's records as graded garments in ``db`` (a copy of its snapshot, or of your database now for
    ``training="live"``), and the held-out seeds. Raises BenchmarkError when live data cannot represent the frozen
    records faithfully."""
    from app.models import Product
    from app.services.provenance import Identity, norm_url

    from .heldout import GradedGarment, TestSet

    ident = Identity(db)
    seeds = bench.manifest["held_out_seeds"]
    garments, problems, changed = [], [], []
    by_identity: dict[tuple, int] = {}
    if training == "live":
        rows = db.query(Product.id, Product.seller_id, Product.external_id)
        by_identity = {(s, e): pid for pid, s, e in rows}
    for r in bench.records:
        lst = r["listing"]
        if lst is None:
            continue
        if training == "snapshot":
            pid = lst["product_id"]
            p = db.get(Product, pid)
            if p is None or (p.seller_id, p.external_id) != (lst["seller_id"], lst["external_id"]):
                problems.append(f"record {r['record']}: its listing is not in the snapshot as frozen")
                continue
        else:
            pid = by_identity.get((lst["seller_id"], lst["external_id"]))
            if pid is None:
                pids = ident.by_url.get(norm_url(lst["source_url"]) or "")
                pid = min(pids) if pids else None
            if pid is None:
                problems.append(f"record {r['record']}: its listing no longer exists")
                continue
            p = db.get(Product, pid)
            if content_fingerprint(p) != r["content"]:
                changed.append(r["record"])
        created = datetime.fromisoformat(r["judged_at"])
        garments.append(GradedGarment(ident.family(pid), r["unit"], r["taste"], pid, r["judgement_ids"], r["grades"],
                                      created, r["snapshot"] or {}))  # fmt: skip
    if training == "live":
        fam_records: dict[str, list[int]] = {}
        for g, r in zip(garments, [r for r in bench.records if r["listing"] is not None], strict=False):
            fam_records.setdefault(g.family, []).append(r["record"])
        merged = [v for v in fam_records.values() if len(v) > 1]
        if merged:
            problems.append(f"records now one garment: {merged}")
        units_now = ident.components([g.family for g in garments])
        frozen_units: dict[str, set[str]] = {}
        for g in garments:
            frozen_units.setdefault(units_now[g.family], set()).add(g.unit)
        joined = [sorted(u) for u in frozen_units.values() if len(u) > 1]
        if joined:
            problems.append(f"split units joined by later matches: {joined}")
    if problems:
        raise BenchmarkError(
            f"{bench.ref} cannot be evaluated on {'its snapshot' if training == 'snapshot' else 'live data'}: "
            + "; ".join(problems[:20]) + (" ..." if len(problems) > 20 else "")
            + (". Evaluate its snapshot (--training snapshot), or freeze a successor version." if training == "live"
               else ". The snapshot does not match the records."))  # fmt: skip
    if training == "snapshot":
        held = {"families": seeds["families"], "product_ids": seeds["product_ids"], "urls": seeds["urls"]}
    else:  # ids may have been reused since: stable identities and addresses only
        pids = [by_identity[(s, e)] for s, e in seeds["listings"] if (s, e) in by_identity]
        for r in bench.records:
            pids += [by_identity[(m["seller_id"], m["external_id"])] for m in r["members"]
                     if (m["seller_id"], m["external_id"]) in by_identity]  # fmt: skip
        held = {"families": [], "product_ids": pids, "urls": seeds["urls"]}
    report = {"judgements": bench.manifest["counts"]["judgements"], "records": len(bench.records),
              "content_changed": changed, "conflicts": bench.manifest["excluded"]["conflicts"],
              "missing": bench.manifest["excluded"]["missing_listing"]}  # fmt: skip
    source = {"kind": "benchmark", "benchmark": bench.ref, "hash": bench.hash, "training": training,
              "frozen_at": bench.manifest["created_at"], "split_seed": bench.manifest["protocol"]["split_seed"]}  # fmt: skip
    return TestSet(garments, report, held, source)


# ----------------------------------------------------------------- evaluation databases
def open_training(bench: Benchmark, training: str, live_src: Path, tmp: Path, cutoff: datetime | None = None):
    """A session on a disposable copy of the training data for a run: the benchmark's snapshot, or your database
    now; with ``cutoff``, everything added later is deleted from the copy."""
    from .dbcopy import open_copy

    if training not in ("snapshot", "live"):
        raise BenchmarkError(f"training must be snapshot or live, not {training!r}")
    if cutoff is not None and training == "snapshot":
        raise BenchmarkError("--cutoff applies to live training data; the snapshot already ends when it was frozen")
    db = open_copy(bench.snapshot if training == "snapshot" else live_src, tmp)
    if cutoff is not None:
        apply_cutoff(db, cutoff)
    return db


def apply_cutoff(db, cutoff: datetime) -> dict:
    """Delete from an evaluation COPY what was added after ``cutoff`` (decisions, wardrobe pieces, inspiration)."""
    from app.models import Feedback, InspirationImage, WardrobeItem

    cutoff = cutoff.replace(tzinfo=None)
    out = {"decisions": db.query(Feedback).filter(Feedback.created_at >= cutoff).delete(synchronize_session=False),
           "wardrobe_pieces": db.query(WardrobeItem).filter(WardrobeItem.added_at >= cutoff)
           .delete(synchronize_session=False),
           "inspiration_photos": db.query(InspirationImage).filter(InspirationImage.added_at >= cutoff)
           .delete(synchronize_session=False)}  # fmt: skip
    db.commit()
    return out


def run_entry(db, evaluator: str, training: str, cutoff: datetime | None, result_path: Path | None) -> dict:
    """What a run declares about itself: training data, configuration and code."""
    from app.services import calibration
    from app.services import embeddings as emb
    from app.services import preference_model as pm
    from app.services.approval_data import LABEL_VERSION
    from app.services.settings_store import get_setting

    settings = {k: get_setting(db, k) for k in ("selective", "recommender", "ranking_weights")}
    return {"evaluator": evaluator, "training": training, "cutoff": cutoff.isoformat() if cutoff else None,
            "data": _data_identity(db), "engine": pm.ENGINE_VERSION, "labels": LABEL_VERSION,
            "calibration_method": calibration.METHOD, "image_backend": emb.get_image_backend().name,
            "settings_sha256": hashlib.sha256(json.dumps(settings, sort_keys=True, default=str).encode()).hexdigest(),
            "code": code_identity(), "result": str(result_path) if result_path else None}  # fmt: skip


# ----------------------------------------------------------------- command line
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", help="database file (default: this installation's)")
    ap.add_argument("--data-dir", help="where benchmarks are kept (default: WS_DATA_DIR)")
    common = argparse.ArgumentParser(add_help=False)  # the same options, accepted after the command too
    common.add_argument("--db", default=argparse.SUPPRESS, help="database file (default: this installation's)")
    common.add_argument("--data-dir", default=argparse.SUPPRESS, help="where benchmarks are kept")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("freeze", parents=[common], help="freeze the current grades as a new benchmark version")
    f.add_argument("--name", default=DEFAULT_NAME)
    f.add_argument("--new-version", action="store_true", help="create a successor version of an existing name")
    f.add_argument("--seed", type=int, default=0, help="split seed (threshold halves)")
    f.add_argument("--from-legacy", action="store_true", help="rebuild from evaluation/frozen_test_set.json")
    sub.add_parser("list", parents=[common], help="list benchmark versions")
    v = sub.add_parser("verify", parents=[common], help="recompute a version's hashes")
    v.add_argument("ref")
    sub.add_parser("legacy", parents=[common], help="what an old ID-only freeze holds")
    args = ap.parse_args(argv)

    from app.config import get_settings

    from .dbcopy import source_database

    data_dir = Path(args.data_dir) if args.data_dir else get_settings().data_dir
    try:
        if args.cmd == "freeze":
            src = source_database(args.db)
            if not src.is_file():
                print(f"database not found: {src}", file=sys.stderr)
                return 2
            legacy = legacy_path(data_dir) if args.from_legacy else None
            if legacy is not None and not legacy.is_file():
                print(f"no ID-only freeze at {legacy}", file=sys.stderr)
                return 2
            m = freeze(src, data_dir, args.name, new_version=args.new_version, seed=args.seed, legacy=legacy)
            c = m["counts"]
            print(f"Froze {c['records']} graded garments ({c['judgements']} judgements, {c['approved']} approved, "
                  f"{c['merged']} merged, {c['conflicts']} left out as conflicting) as {m['name']}@v{m['version']}"
                  f" · hash {m['content_sha256'][:16]}\n{root(data_dir) / m['name'] / ('v' + str(m['version']))}")  # fmt: skip
            return 0
        if args.cmd == "list":
            base = root(data_dir)
            names = sorted(p.name for p in base.iterdir() if p.is_dir()) if base.is_dir() else []
            for name in names:
                for ver in versions(data_dir, name):
                    try:
                        b = load(data_dir, f"{name}@v{ver}", verify_snapshot=False)
                        c = b.manifest["counts"]
                        print(f"{b.ref}  {b.manifest['created_at'][:19]}  {c['records']} garments  "
                              f"hash {b.hash[:16]}  runs {len(runs(b))}")  # fmt: skip
                    except BenchmarkError as e:
                        print(f"{name}@v{ver}  NOT USABLE: {e}")
            if not names:
                print("No benchmarks yet: python -m eval.benchmark freeze --name <name>")
            return 0
        if args.cmd == "verify":
            b = load(data_dir, args.ref)
            print(f"{b.ref}: records, snapshot and manifest match their hashes · hash {b.hash[:16]}")
            return 0
        if args.cmd == "legacy":
            path = legacy_path(data_dir)
            if not path.is_file():
                print("No ID-only freeze found.")
                return 0
            src = source_database(args.db)
            with tempfile.TemporaryDirectory(prefix="ws-bench-") as tmp:
                from app.models import Judgement

                from .dbcopy import close_copy, open_copy

                db = open_copy(src, Path(tmp))
                try:
                    print(json.dumps(legacy_status(path, db.query(Judgement).all()), indent=1))
                finally:
                    close_copy(db)
            print("An ID-only freeze cannot be verified and is not used as a benchmark. To rebuild it as one: "
                  "python -m eval.benchmark freeze --name <name> --from-legacy")  # fmt: skip
            return 0
    except BenchmarkError as e:
        print(str(e), file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
