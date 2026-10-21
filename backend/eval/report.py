"""Summarise simulation logs: precision@10 per persona and method.

Usage: .venv/bin/python -m eval.report results.log [more.log ...]
       .venv/bin/python -m eval.report --paired METHOD_A METHOD_B results.log [more.log ...]
"""

from __future__ import annotations

import json
import statistics as st
import sys
from collections import defaultdict


def summarise(paths: list[str]) -> str:
    rows = [json.loads(line) for path in paths for line in open(path) if line.startswith("{")]
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        groups[(r["persona"], r["method"])].append(r)
    lines = [
        f"{'persona':<11}{'method':<20}{'relevant':>9}  {'P@10 r1':>7}{'r2-4':>7}{'r5-8':>7}{'all':>7}  {'runs':>4}"
    ]
    for (persona, method), rs in sorted(groups.items()):
        prec = [r["prec"] for r in rs]
        r1 = st.mean(x[0] for x in prec)
        r24 = st.mean(st.mean(x[1:4]) for x in prec)
        r58 = st.mean(st.mean(x[4:]) for x in prec)
        overall = st.mean(st.mean(x) for x in prec)
        relevant = st.mean(r["rel"] for r in rs) / 360
        lines.append(
            f"{persona:<11}{method:<20}{relevant:>9.0%}  {r1:>7.2f}{r24:>7.2f}{r58:>7.2f}{overall:>7.2f}  {len(rs):>4}"
        )
    return "\n".join(lines)


def paired(paths: list[str], a: str, b: str, boot: int = 5000, seed: int = 0) -> str:
    """Per persona: mean of (a − b) per-seed precision@10, a 95% bootstrap interval over seeds, and wins."""
    import random

    rows = [json.loads(line) for path in paths for line in open(path) if line.startswith("{")]
    by = {(r["persona"], r["method"], r["seed"]): st.mean(r["prec"]) for r in rows}
    rng = random.Random(seed)
    lines = [f"| Persona | {a} − {b} [95% CI] | wins |", "|---|---|---|"]
    for persona in sorted({k[0] for k in by}):
        seeds = sorted(s for (p, m, s) in by if p == persona and m == a and (p, b, s) in by)
        if not seeds:
            continue
        d = [by[(persona, a, s)] - by[(persona, b, s)] for s in seeds]
        means = sorted(st.mean(rng.choice(d) for _ in d) for _ in range(boot))
        lo, hi = means[int(0.025 * boot)], means[int(0.975 * boot) - 1]
        wins = sum(x > 0 for x in d)
        lines.append(f"| {persona} | {st.mean(d):+.3f} [{lo:+.3f}, {hi:+.3f}] | {wins}/{len(d)} |")
    return "\n".join(lines)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--paired"]:
        print(paired(sys.argv[4:], sys.argv[2], sys.argv[3]))
    else:
        print(summarise(sys.argv[1:]))
