"""The pilot workflow (eval/pilot.py).

SYNTHETIC FIXTURE (tests/test_eval_selective.timed_world): decisions and grades drawn from known approval
probabilities. These tests check the workflow and the honesty of the report (requirements, refusals, the sample-size
table, the photo-analysis statement, comparisons at equal coverage), not how well the app predicts anyone's taste.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from .test_eval_selective import timed_world

BACKEND = Path(__file__).resolve().parents[1]


def _cli(env: Path, module: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", module, "--db", str(env / "test.db"), "--data-dir", str(env), *args],
                          cwd=BACKEND, env=os.environ.copy(), capture_output=True, text=True, timeout=900)  # fmt: skip


def test_the_plan_says_what_a_pilot_still_needs(env, db):
    from eval.pilot import plan

    timed_world(db, per=30, rated=18, judged=8)
    p = plan(db)
    assert p["products"]["status"] == "ok" and 150 <= p["products"]["have"] <= 300
    assert p["graded"]["have"] == 48 and p["graded"]["status"] == "ok"
    assert p["training_decisions"]["have"] == 108 and p["training_decisions"]["status"] == "ok"
    assert p["exploration_decisions"] == {"have": 0, "status": "short", "need": 10, "range": [10, None]}
    assert p["image_backend"]["now"].startswith("lite") and p["exposures"]["have"] == 0


def test_the_sample_size_table_matches_clopper_pearson():
    from eval.pilot import clopper_pearson_table

    t = {r["selected"]: r["all_approved_lower_bound"] for r in clopper_pearson_table()}
    assert t[30] == pytest.approx(0.8843, abs=1e-4)
    assert t[60] == pytest.approx(0.9404, abs=1e-4)
    assert t[72] >= 0.95 > t[60]


@pytest.mark.parametrize("console_encoding", [None, "ascii"])
def test_the_report_on_a_frozen_benchmark(env, db, monkeypatch, console_encoding):
    if console_encoding:
        monkeypatch.setenv("PYTHONIOENCODING", console_encoding)
    timed_world(db, per=30, rated=18, judged=8)
    r = _cli(env, "eval.benchmark", "freeze", "--name", "pilot")
    assert r.returncode == 0, r.stderr[-2000:]
    r = _cli(env, "eval.pilot", "report", "--benchmark", "pilot@v1")
    assert r.returncode == 0, r.stderr[-3000:]
    assert "# Pilot report" in r.stdout  # legacy consoles may escape unsupported mathematical symbols
    out = sorted((env / "evaluation" / "pilot").glob("pilot-v1-*.md"))[-1].read_text(encoding="utf-8")
    for section in ("## The pilot's requirements", "## How much a result can show", "## Your graded garments",
                    "## What Discover served and you saw", "## Taste and quality", "## What this does not show"):  # fmt: skip
        assert section in out, section
    assert "not CLIP" in out and "Nothing in this report says how CLIP would do" in out
    assert "| … of them on Learning picks | 0 | ≥ 10 | 10 short |" in out
    assert "| 72 | 95.0% |" in out and "| 60 | 94.0% |" in out
    assert "Variety of what you saw: unavailable" in out  # no exposure recorded under the current definition

    rep = json.loads(sorted((env / "evaluation" / "pilot").glob("pilot-v1-*.json"))[-1].read_text())
    assert rep["benchmark"]["ref"] == "pilot@v1" and rep["benchmark"]["training"] == "snapshot"
    rep_modes = rep["selective"]["representative"]["modes"]
    for mode in ("balanced", "high", "extreme"):
        g = rep_modes[mode]
        n = g["gate"]["selected"]  # every comparison takes as many pieces as the gate did
        for other in ("written_profile_same_coverage", "taste_ranking_same_coverage", "discover_score_same_coverage"):
            assert g[other]["selected"] == n, (mode, other)
    assert set(rep["selective"]["representative"]["categories"]) == {"overcoat", "trousers", "hoodie"}
    runs = [json.loads(x) for x in (env / "evaluation" / "benchmarks" / "pilot" / "runs.jsonl").read_text().splitlines()]
    assert runs[-1]["evaluator"] == "eval.pilot"


def test_too_few_grades_are_reported_as_not_measured(env, db):
    timed_world(db, per=30, rated=18, judged=2)  # 12 graded garments
    assert _cli(env, "eval.benchmark", "freeze", "--name", "pilot").returncode == 0
    r = _cli(env, "eval.pilot", "report", "--benchmark", "pilot@v1")
    assert r.returncode == 0, r.stderr[-3000:]
    assert "Not measured: needs 40 graded garments" in r.stdout
    assert "| Graded garments (held out) | 12 | 40–60 | 28 short |" in r.stdout
