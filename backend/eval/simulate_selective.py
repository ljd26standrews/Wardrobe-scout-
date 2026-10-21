"""Simulated users meet the selective gate: what each mode shows, how often it is right, and how long it takes
to show anything at all.

SYNTHETIC. The catalogue and personas of eval/simulate.py (360 drawn items; three personas with hidden utility
functions). These numbers describe the gate's behaviour in a toy world. They are not evidence of real-world
precision, and the mode targets stay unvalidated until real outcomes confirm them (eval/selective.py).

Usage (from backend/):
    .venv/bin/python -m eval.simulate_selective [personas] [policies] [seeds] [rounds] > selective.log
    .venv/bin/python -m eval.simulate_selective --report selective.log [more.log ...]   # summary, then like for like

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
            out.append({**row, "main": tally(row["main"]), "learning": tally(row["learning"])})
    if policy == "high-nocal":
        from app.services import calibration

        calibration.MIN_BRIER_GAIN = 0.001
    return {"persona": persona, "policy": policy, "seed": seed, "rounds": out}


def tally(pieces: list[dict]) -> list:
    """[shown, wanted, rated, approved, sum of estimates over rated, rated with an estimate]: a skip is not rated."""
    rated = [p for p in pieces if p["action"] != "skip"]
    est = [p["estimate"] for p in rated if p["estimate"] is not None]
    approved = sum(p["action"] in ("love", "like") for p in rated)
    return [len(pieces), sum(p["wanted"] for p in pieces), len(rated), approved, round(sum(est), 4), len(est)]


# ----------------------------------------------------------------- report
def _cp(k: int, n: int) -> str:
    from app.services.selective import clopper_pearson

    if n == 0:
        return "–"
    lo, hi = clopper_pearson(k, n)
    return f"{k / n:.0%} [{lo:.0%}, {hi:.0%}]"


def _load(paths: list[str]) -> list[dict]:
    rows = [json.loads(line) for path in paths for line in open(path) if line.startswith("{")]
    for r in rows:  # older logs kept every piece; tally them the same way
        for rnd in r["rounds"]:
            for kind in ("main", "learning"):
                if rnd[kind] and isinstance(rnd[kind][0], dict):
                    rnd[kind] = tally(rnd[kind])
                elif not rnd[kind]:
                    rnd[kind] = tally([])
    return rows


def _sum(tallies) -> list:
    tallies = list(tallies)  # summed column by column: a generator would be spent after the first
    return [sum(t[i] for t in tallies) for i in range(6)]


def _rate(t: list, num: int, den: int) -> str:
    return _cp(t[num], t[den])


def summarise(paths: list[str]) -> str:
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in _load(paths):
        groups[(r["persona"], r["policy"])].append(r)
    lines = [
        "SYNTHETIC simulation (eval/simulate_selective.py): gate behaviour in a toy world, not real-world precision.",
        "",
        "| Persona | Policy | Runs | Main feed shown / round | Main: wanted / shown | Main: approved / rated | "
        "First round with main pieces | Learning picks: approved / rated |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for (persona, policy), rs in sorted(groups.items()):
        main = _sum(rnd["main"] for r in rs for rnd in r["rounds"])
        learn = _sum(rnd["learning"] for r in rs for rnd in r["rounds"])
        firsts = [next((rnd["round"] for rnd in r["rounds"] if rnd["main"][0]), None) for r in rs]
        shown_per_round = st.mean(rnd["main"][0] for r in rs for rnd in r["rounds"])
        first = "never" if all(f is None for f in firsts) else (
            f"{st.median(f for f in firsts if f is not None):.0f}" + ("" if all(firsts) else " (some never)")
        )  # fmt: skip
        lines.append(f"| {persona} | {policy} | {len(rs)} | {shown_per_round:.1f} | {_rate(main, 1, 0)} | "
                     f"{_rate(main, 3, 2)} | {first} | {_rate(learn, 3, 2)} |")  # fmt: skip
    lines += ["", "Intervals: 95% Clopper-Pearson over pooled pieces (pieces within a run are not independent, so they "
              "are optimistic). A skip is not a rating."]  # fmt: skip
    return "\n".join(lines)


def phases(paths: list[str], policy: str = "balanced") -> str:
    """Like for like: ``policy``'s main feed against the earlier ranked feed in the same rounds of the same seed,
    split by whether ``policy`` had a calibration in that round."""
    rows = _load(paths)
    ranked = {(r["persona"], r["seed"]): r for r in rows if r["policy"] == "ranked"}
    acc: dict[tuple[str, str], list[list]] = defaultdict(lambda: [[], []])
    for r in rows:
        if r["policy"] != policy or (r["persona"], r["seed"]) not in ranked:
            continue
        for rnd, base in zip(r["rounds"], ranked[(r["persona"], r["seed"])]["rounds"], strict=False):
            phase = "calibrated" if rnd["state"] == "calibrated" else "not yet calibrated"
            acc[(r["persona"], phase)][0].append(rnd["main"])
            acc[(r["persona"], phase)][1].append(base["main"])
    lines = [f"| Persona | Rounds | {policy}: approved / rated | Mean estimate | Earlier ranked feed, same rounds |",
             "|---|---|---|---|---|"]  # fmt: skip
    for (persona, phase), (mine, base) in sorted(acc.items()):
        m, b = _sum(mine), _sum(base)
        est = f"{m[4] / m[5]:.2f}" if m[5] else "–"
        shown = _rate(m, 3, 2) if m[2] else "nothing shown"
        lines.append(f"| {persona} | {phase} ({len(mine)}) | {shown} | {est} | {_rate(b, 3, 2)} |")
    return "\n".join(lines)


def main(argv: list[str]) -> None:
    if argv and argv[0] == "--report":
        print(summarise(argv[1:]))
        print()
        print(phases(argv[1:]))
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
