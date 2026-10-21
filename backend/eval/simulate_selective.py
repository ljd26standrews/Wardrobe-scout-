"""Simulated users meet the selective gate: what each mode shows, how often it is right, and how long it takes
to show anything at all.

SYNTHETIC. The catalogue and personas of eval/simulate.py (360 drawn items; three personas with hidden utility
functions). These numbers describe the gate's behaviour in a toy world. They are not evidence of real-world
precision, and the mode targets stay unvalidated until real outcomes confirm them (eval/selective.py).

Usage (from backend/):
    .venv/bin/python -m eval.simulate_selective [personas] [policies] [seeds] [rounds] > selective.log
    .venv/bin/python -m eval.simulate_selective --report selective.log [more.log ...]

Policies:
    ranked                  the earlier feed: top 10 by score, with exploration slots (selective off)
    balanced, high, extreme the gated main feed in that mode, topped up with Learning picks
    high-nounc              High confidence gating on the estimate alone (confidence 0.5): no uncertainty
    high-nocal              High confidence with the identity map forced: no calibration

Each round the persona is shown 10 pieces: the main feed first and, when the gate shows fewer, Learning picks for
the rest (as Discover invites). It loves or likes (utility >= 0.7), skips (0.5-0.7) or dislikes each one, with the
reason a person would give. Approval is love or like; a skip is not a rating (as in the app's labels).
"""

from __future__ import annotations

import json
import statistics as st
import sys
from collections import defaultdict

from .simulate import PERSONAS, RELEVANT, catalogue, decide, normalise, world

K = 10
ABLATIONS = {"high-nounc": ("high", {"high_confidence": 0.5}), "high-nocal": ("high", {})}


def run(persona: str, policy: str, seed: int, rounds: int = 15, k: int = K) -> dict:
    items = catalogue(seed)
    fn = PERSONAS[persona]
    util = normalise({it["id"]: fn(it) for it in items})
    by_ext = {it["id"]: it for it in items}
    mode, overrides = ABLATIONS.get(policy, (policy, {}))
    out = []
    with world(seed, selective=policy != "ranked") as (c, ext_of):
        from app.services import calibration, jobs

        jobs.enqueue = lambda kind, params=None: jobs.run_sync(kind, params)  # recalibrate before the next round
        if policy == "high-nocal":
            calibration.MIN_BRIER_GAIN = float("inf")  # Platt is never preferred: the identity map always
        if overrides:
            cfg = c.get("/api/settings").json()["selective"]
            assert c.put("/api/settings/selective", json={**cfg, **overrides}).status_code == 200
        for rnd in range(rounds):
            if policy == "ranked":
                feed = c.get(f"/api/feed?limit={k}").json()
                main, learning = feed["items"], []
                state = "ranked"
            else:
                feed = c.get(f"/api/feed?limit={k}&mode={mode}").json()
                main, state = feed["items"], feed["gate"]["state"]
                learning = []
                if len(main) < k:
                    lf = c.get(f"/api/feed/learning?limit={k - len(main)}&mode={mode}").json()
                    learning = lf["items"][: k - len(main)]
            row = {"round": rnd + 1, "state": state, "main": [], "learning": []}
            for kind, shown, fid in (("main", main, feed.get("feed_id")), ("learning", learning, None)):
                for pos, x in enumerate(shown):
                    it = by_ext[ext_of[x["id"]]]
                    action, reasons = decide(it, util[it["id"]], fn)
                    ctx = {"feed_id": fid, "position": pos, "explore": kind == "learning"}
                    c.post("/api/feedback", json={"product_id": x["id"], "action": action, "reasons": reasons,
                                                  "context": ctx})  # fmt: skip
                    row[kind].append({"wanted": util[it["id"]] >= RELEVANT, "action": action,
                                      "estimate": (x.get("confidence") or {}).get("estimate")})  # fmt: skip
            out.append(row)
    if policy == "high-nocal":
        from app.services import calibration

        calibration.MIN_BRIER_GAIN = 0.001
    return {"persona": persona, "policy": policy, "seed": seed, "rounds": out}


# ----------------------------------------------------------------- report
def _cp(k: int, n: int) -> str:
    from app.services.selective import clopper_pearson

    if n == 0:
        return "–"
    lo, hi = clopper_pearson(k, n)
    return f"{k / n:.0%} [{lo:.0%}, {hi:.0%}]"


def summarise(paths: list[str]) -> str:
    rows = [json.loads(line) for path in paths for line in open(path) if line.startswith("{")]
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        groups[(r["persona"], r["policy"])].append(r)
    lines = [
        "SYNTHETIC simulation (eval/simulate_selective.py): gate behaviour in a toy world, not real-world precision.",
        "",
        "| Persona | Policy | Runs | Main feed shown / round | Main: wanted / shown | Main: approved / rated | "
        "First round with main pieces | Learning picks: approved / rated |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for (persona, policy), rs in sorted(groups.items()):
        main = [p for r in rs for rnd in r["rounds"] for p in rnd["main"]]
        learn = [p for r in rs for rnd in r["rounds"] for p in rnd["learning"]]
        rated = [p for p in main if p["action"] != "skip"]
        lrated = [p for p in learn if p["action"] != "skip"]
        firsts = [next((rnd["round"] for rnd in r["rounds"] if rnd["main"]), None) for r in rs]
        shown_per_round = st.mean(len(rnd["main"]) for r in rs for rnd in r["rounds"])
        first = "never" if all(f is None for f in firsts) else (
            f"{st.median(f for f in firsts if f is not None):.0f}" + ("" if all(firsts) else " (some never)")
        )  # fmt: skip
        lines.append(
            f"| {persona} | {policy} | {len(rs)} | {shown_per_round:.1f} | "
            f"{_cp(sum(p['wanted'] for p in main), len(main))} | "
            f"{_cp(sum(p['action'] in ('love', 'like') for p in rated), len(rated))} | {first} | "
            f"{_cp(sum(p['action'] in ('love', 'like') for p in lrated), len(lrated))} |"
        )
    lines += ["", "Intervals: 95% Clopper-Pearson over pooled pieces (pieces within a run are not independent, so they "
              "are optimistic). A skip is not a rating."]  # fmt: skip
    return "\n".join(lines)


def main(argv: list[str]) -> None:
    if argv and argv[0] == "--report":
        print(summarise(argv[1:]))
        return
    personas = argv[0].split(",") if argv else list(PERSONAS)
    policies = argv[1].split(",") if len(argv) > 1 else ["ranked", "balanced", "high", "extreme"]
    seeds = [int(s) for s in argv[2].split(",")] if len(argv) > 2 else [2, 3, 4]
    rounds = int(argv[3]) if len(argv) > 3 else 15
    for persona in personas:
        for policy in policies:
            for seed in seeds:
                print(json.dumps(run(persona, policy, seed, rounds)), flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
