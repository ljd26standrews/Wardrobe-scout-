"""Guard against ranking regressions using the offline simulation (eval/).

Runs in a subprocess because the simulation configures its own database.
Uses the 'stated' persona, where the Bayesian engine beat both baselines on
every held-out seed (eval/RESULTS.md), so a failure here is a real signal.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]

SCRIPT = """
import json, eval.simulate as s
out = {m: sum(x["precision"] for x in s.run("stated", m, 2, rounds=6)["rounds"]) / 6
       for m in ("rules_only", "legacy", "bayes")}
print("RESULT" + json.dumps(out))
"""


def test_bayes_engine_beats_baselines_on_simulated_user():
    proc = subprocess.run(
        [sys.executable, "-c", SCRIPT],
        cwd=BACKEND,
        env={**{k: v for k, v in os.environ.items()
                if k.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "COMSPEC", "PATHEXT"}},
             "PYTHONPATH": str(BACKEND)},
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    line = next(x for x in proc.stdout.splitlines() if x.startswith("RESULT"))
    p = json.loads(line[len("RESULT") :])
    assert p["bayes"] > p["rules_only"], p
    assert p["bayes"] > p["legacy"], p
