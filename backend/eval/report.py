"""Summarise simulation logs: precision@10 per persona and method.

Usage: .venv/bin/python -m eval.report results.log [more.log ...]
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


if __name__ == "__main__":
    print(summarise(sys.argv[1:]))
