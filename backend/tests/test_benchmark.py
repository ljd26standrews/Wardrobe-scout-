"""Frozen, versioned benchmarks (eval/benchmark.py), through the real command-line workflow: freeze, evaluate with
both evaluators, change the live database in every way that used to change a frozen test set, evaluate again.
Synthetic catalogue, disposable databases."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from .test_evaluation import _catalogue, _grade, _judge

BACKEND = Path(__file__).resolve().parents[1]


def _cli(env: Path, module: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", module, "--db", str(env / "test.db"), "--data-dir", str(env), *args],
                          cwd=BACKEND, env=os.environ.copy(), capture_output=True, text=True, timeout=900)  # fmt: skip


def _ok(r: subprocess.CompletedProcess) -> str:
    assert r.returncode == 0, (r.stdout[-2000:], r.stderr[-3000:])
    return r.stdout


def _freeze(env: Path, *extra: str) -> subprocess.CompletedProcess:
    return _cli(env, "eval.benchmark", "freeze", *extra)


def _latest(env: Path, pattern: str) -> dict:
    return json.loads(sorted((env / "evaluation").glob(pattern))[-1].read_text())


def _world(db, judged=45):
    from app.models import Feedback

    ps = _catalogue(db, n_per_seller=20)
    for p in ps[:12]:
        db.add(Feedback(product_id=p.id, action="love" if _grade(p) >= 2 else "dislike"))
    db.commit()
    _judge(db, ps[12 : 12 + judged], _grade)
    return ps


def _version_dir(env: Path, ref: str = "default@v1") -> Path:
    name, v = ref.split("@")
    return env / "evaluation" / "benchmarks" / name / v


def test_freezing_again_refuses_unless_a_successor_is_asked_for(env, db):
    _world(db)
    out = _ok(_freeze(env))
    assert "45 graded garments" in out and "default@v1" in out
    v1 = _version_dir(env)
    manifest = json.loads((v1 / "manifest.json").read_text())
    files = {f: (v1 / f).read_bytes() for f in ("manifest.json", "records.jsonl", "snapshot.db")}
    for f in files:
        assert not os.stat(v1 / f).st_mode & stat.S_IWUSR  # written once, then read-only

    again = _freeze(env)
    assert again.returncode == 3 and "never overwritten" in again.stderr and "--new-version" in again.stderr
    out = _ok(_freeze(env, "--new-version"))
    assert "default@v2" in out
    v2 = json.loads((_version_dir(env, "default@v2") / "manifest.json").read_text())
    assert v2["parent"] == "default@v1" and v2["version"] == 2
    assert {f: (v1 / f).read_bytes() for f in files} == files  # v1 untouched
    assert manifest["counts"]["records"] == 45 and len((v1 / "records.jsonl").read_text().splitlines()) == 45
    for key in ("created_at", "training_cutoff", "protocol", "records_sha256", "snapshot", "data", "profile",
                "representation", "engine", "labels", "code", "content_sha256"):  # fmt: skip
        assert manifest.get(key) is not None, key
    record = json.loads((v1 / "records.jsonl").read_text().splitlines()[0])
    assert {"seller_id", "external_id", "source_url"} <= set(record["listing"]) and record["grades"]
    assert record["split"] in (0, 1) and record["snapshot"]["title"]


def test_undo_regrading_id_reuse_imports_and_product_changes_cannot_substitute_records(env, db):
    """Measure on default@v1, then change the live database every way that used to change a frozen test set (an
    Undo whose id is reused by a new grade, a re-grade, a changed listing, an import, more decisions): measuring on
    default@v1 again gives exactly the same records and results."""
    from app.ingestion.base import RawListing
    from app.ingestion.pipeline import upsert_listing
    from app.models import Feedback, Judgement, Product, Seller

    ps = _world(db)
    _ok(_freeze(env))
    _ok(_cli(env, "eval.real", "--benchmark", "default@v1"))
    first = _latest(env, "results-*.json")
    assert first["summary"]["test_set"]["benchmark"] == "default@v1"

    last = db.query(Judgement).order_by(Judgement.id.desc()).first()
    undone_id, undone_product = last.id, last.product_id
    db.delete(last)  # Undo on the Evaluate page
    db.commit()
    replacement = Judgement(product_id=ps[58].id, family_key=f"p{ps[58].id}", taste=3)
    db.add(replacement)
    db.commit()
    assert replacement.id == undone_id  # SQLite reuses the id: an ID-only freeze would now include this grade
    regraded = db.query(Judgement).filter(Judgement.product_id == ps[13].id).one()
    regraded.taste = 3 - regraded.taste
    changed = db.get(Product, ps[14].id)
    changed.title_en, changed.colours = "red wool coat", ["red"]
    upsert_listing(db, db.query(Seller).first(), RawListing("new-import", "https://shop.example/new", "navy wool coat"))
    db.add(Feedback(product_id=ps[59].id, action="love"))
    db.commit()

    _ok(_cli(env, "eval.real", "--benchmark", "default@v1"))
    second = _latest(env, "results-*.json")
    assert second["summary"]["test_set_hash"] == first["summary"]["test_set_hash"]
    assert second["all"] == first["all"]  # same records, same training snapshot: the same measurement
    records = [json.loads(x) for x in (_version_dir(env) / "records.jsonl").read_text().splitlines()]
    products = {r["listing"]["product_id"] for r in records}
    assert undone_product in products and ps[58].id not in products

    # Your current grades (unfrozen) do see the changes: that is the difference.
    _ok(_cli(env, "eval.real"))
    assert _latest(env, "results-*.json")["summary"]["test_set_hash"] != first["summary"]["test_set_hash"]


def test_both_evaluators_consume_the_version_and_report_its_hash(env, db):
    _world(db)
    _ok(_freeze(env, "--name", "pilot"))
    manifest = json.loads((_version_dir(env, "pilot@v1") / "manifest.json").read_text())
    out_real = _ok(_cli(env, "eval.real", "--benchmark", "pilot@v1"))
    out_sel = _ok(_cli(env, "eval.selective", "--benchmark", "pilot@v1"))
    h = manifest["content_sha256"][:16]
    assert "benchmark pilot@v1" in out_real and h in out_real
    assert "benchmark pilot@v1" in out_sel and h in out_sel
    rep = _latest(env, "selective-*.json")["representative"]["summary"]
    real = _latest(env, "results-*.json")["summary"]
    assert rep["test_set"]["hash"] == real["test_set"]["hash"] == manifest["content_sha256"]
    assert rep["garments"] == real["garments"] == 45
    runs = [json.loads(x) for x in (env / "evaluation" / "benchmarks" / "pilot" / "runs.jsonl").read_text().splitlines()]
    assert [r["evaluator"] for r in runs] == ["eval.real", "eval.selective"]
    for r in runs:  # each run declares its training data and configuration
        assert r["benchmark"] == "pilot@v1" and r["training"] == "snapshot" and r["engine"] and r["settings_sha256"]


def test_live_training_refuses_records_that_have_since_become_one_garment(env, db):
    from app.services import dedup

    ps = _world(db)
    _ok(_freeze(env))
    dedup.set_user_decision(db, ps[12].id, ps[13].id, same=True)  # two graded listings: one garment after all
    r = _cli(env, "eval.real", "--benchmark", "default@v1", "--training", "live")
    assert r.returncode == 3 and "now one garment" in r.stderr and "successor" in r.stderr
    _ok(_cli(env, "eval.real", "--benchmark", "default@v1"))  # its snapshot is unaffected


def test_live_training_finds_listings_by_stable_identity_not_by_id(env, db):
    """A graded listing deleted and its id reused by a different listing: live training refuses rather than
    measuring the newcomer in its place; re-imported at its address, it is found again and reported as changed."""
    from app.ingestion.base import RawListing
    from app.ingestion.pipeline import upsert_listing
    from app.models import Product, Seller

    ps = _world(db, judged=48)
    _ok(_freeze(env))
    graded = ps[59]  # the newest listing: SQLite gives its id to the next one
    url, seller_id, pid = graded.source_url, graded.seller_id, graded.id
    db.delete(db.get(Product, pid))
    db.commit()
    other = upsert_listing(db, db.get(Seller, seller_id), RawListing("other", "https://shop.example/other", "red coat"))
    db.commit()
    assert other.product.id == pid
    r = _cli(env, "eval.real", "--benchmark", "default@v1", "--training", "live")
    assert r.returncode == 3 and "no longer exists" in r.stderr

    db.delete(db.get(Product, pid))
    db.commit()
    upsert_listing(db, db.get(Seller, seller_id), RawListing("again", url, "grey wool coat, re-listed", price=999))
    db.commit()
    _ok(_cli(env, "eval.real", "--benchmark", "default@v1", "--training", "live"))
    s = _latest(env, "results-*.json")["summary"]
    assert s["test_set"]["training"] == "live" and s["garments"] == 48


def test_a_benchmark_changed_after_freezing_is_refused(env, db):
    _world(db)
    _ok(_freeze(env))
    path = _version_dir(env) / "records.jsonl"
    os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
    lines = path.read_text().splitlines()
    rec = json.loads(lines[0])
    rec["taste"] = 3 - rec["taste"]
    path.write_text("\n".join([json.dumps(rec, sort_keys=True), *lines[1:]]) + "\n")
    for module in ("eval.real", "eval.selective"):
        r = _cli(env, module, "--benchmark", "default@v1")
        assert r.returncode == 3 and "changed after it was frozen" in r.stderr
    v = _cli(env, "eval.benchmark", "verify", "default@v1")
    assert v.returncode == 3


def test_an_id_only_freeze_is_reported_never_used_and_can_be_rebuilt_honestly(env, db):
    from app.models import Judgement, utcnow

    ps = _world(db)
    ids = [j.id for j in db.query(Judgement).order_by(Judgement.id)]
    legacy = env / "evaluation" / "frozen_test_set.json"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(json.dumps({"created_at": utcnow().isoformat(), "judgement_ids": ids}))
    last = db.query(Judgement).order_by(Judgement.id.desc()).first()
    db.delete(last)
    db.commit()
    db.add(Judgement(product_id=ps[58].id, family_key=f"p{ps[58].id}", taste=3))  # takes the freed id
    db.commit()

    out = _ok(_cli(env, "eval.real"))
    assert "ID-only test set" in out and "1 of its ids now name grades made after it" in out
    assert "not frozen" in out
    status = _ok(_cli(env, "eval.benchmark", "legacy"))
    assert '"reused_after_freeze": [' in status and str(last.id) in status
    out = _ok(_freeze(env, "--name", "rebuilt", "--from-legacy"))
    manifest = json.loads((_version_dir(env, "rebuilt@v1") / "manifest.json").read_text())
    assert manifest["counts"]["records"] == 44  # the reused id is not the frozen grade, so it is left out
    assert manifest["legacy"]["reused_after_freeze"] == [last.id] and "reused id" in manifest["legacy"]["note"]
    assert "rebuilt@v1" in out


@pytest.mark.parametrize("ref", ["missing", "default@v9", "../x", "default@2"])
def test_unknown_or_malformed_versions_are_refused(env, db, ref):
    _world(db, judged=40)
    _ok(_freeze(env))
    r = _cli(env, "eval.real", "--benchmark", ref)
    assert r.returncode == 3 and r.stderr.strip()


def test_full_snapshot_keeps_frozen_benchmark_bytes_and_restore_migrates(env, db, tmp_path, monkeypatch):
    """A restored installation can still verify/evaluate its frozen sample, with identical bytes and grades."""
    from app import db as dbmod
    from app.config import get_settings
    from app.services.backup import restore_full_snapshot, write_full_snapshot
    from eval.benchmark import freeze, load

    _world(db)
    freeze(env / "test.db", env, "restore-pilot")
    before = load(env, "restore-pilot@v1")
    run_log = before.path.parent / "runs.jsonl"
    run_log.write_text('{"test": "synthetic run history"}\n')
    snap = tmp_path / "portable.zip"
    write_full_snapshot(env / "test.db", env, snap)
    restored = tmp_path / "restored-beta"
    result = restore_full_snapshot(snap, restored)
    after = load(restored, before.ref)  # verifies both the records AND snapshot hashes
    assert after.hash == before.hash and after.records == before.records
    assert after.snapshot.read_bytes() == before.snapshot.read_bytes()
    assert (after.path.parent / "runs.jsonl").read_bytes() == run_log.read_bytes()
    assert not (after.path / "records.jsonl").stat().st_mode & stat.S_IWUSR
    monkeypatch.setenv("WS_DATA_DIR", str(restored))
    monkeypatch.setenv("WS_DATABASE_URL", f"sqlite:///{result['database']}")
    get_settings.cache_clear()
    dbmod.init_engine(get_settings().database_url)
    dbmod.migrate()
    from app.models import Feedback, Judgement
    from app.services import revisions

    with dbmod.SessionLocal() as session:
        assert session.query(Judgement).count() == 45
        assert session.query(Feedback).count() == 12
        assert revisions.current(session, "products")[0] != revisions.current(db, "products")[0]
    # The live database can migrate without rewriting a frozen benchmark's database.
    assert load(restored, before.ref).hash == before.hash
