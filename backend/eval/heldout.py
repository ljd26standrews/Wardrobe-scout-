"""The evaluators' shared test-set contract (eval.real and eval.selective's representative protocol).

Test set: your Evaluate grades, one record per garment. Grades that are about one garment now (the same family as
recorded or as it is now, or the same address) are one record, so a garment never counts twice:
* grades that agree on approval (2-3 approved, 0-1 not) are one record with the earliest grade (graded first,
  before any other listing of the garment could have been seen);
* grades that disagree are left out, and reported with their judgement ids: either the match is wrong or the grades
  are, and neither can be resolved here.
Garments that may become one later (services/provenance.py: a single shared photograph) stay separate records but
share a split unit, so threshold halves and bootstrap resamples keep them together.

Training: on a copy of your database, everything the shared held-out policy links to a graded garment (decisions
under any of its listings or addresses, linked wardrobe pieces, inspiration photos that are its photographs) is
deleted before any method is fitted, so no engine, profile or score component can see it. Calibration's out-of-fold
fits leave out the same things without deleting anything; tests/test_heldout.py checks the two agree.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

APPROVED = 2  # a grade of 2 (like) or 3 (love) is an approval


@dataclass
class GradedGarment:
    family: str  # the garment's family key now (or as recorded, when no listing of it exists)
    unit: str  # split and resampling unit: garments that may become one share it
    taste: int  # the grade used (see the module docstring)
    product_id: int | None  # the listing scored (None: no listing of it exists any more)
    judgement_ids: list[int]
    grades: list[int]
    created_at: datetime
    snapshot: dict = field(default_factory=dict)

    @property
    def approved(self) -> int:
        return int(self.taste >= APPROVED)


def graded_garments(db, judgements, ident) -> tuple[list[GradedGarment], dict]:
    """Group ``judgements`` into one record per garment (see the module docstring). Returns the records (oldest
    first) and a report: merged groups, conflicts left out, grades whose listing no longer exists."""
    from app.services.provenance import norm_url

    judgements = sorted(judgements, key=lambda j: (j.created_at, j.id))
    parent = list(range(len(judgements)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    owner: dict[tuple, int] = {}
    for i, j in enumerate(judgements):
        url = norm_url((j.snapshot or {}).get("source_url")) or (ident.url_of.get(j.product_id) if j.product_id else None)
        tokens = {("f", j.family_key)}
        if j.product_id in ident.live:
            tokens.add(("f", ident.family(j.product_id)))
        if url:
            tokens.add(("u", url))
            if (now := ident.family_of_url(url)) is not None:
                tokens.add(("f", now))
        for t in tokens:
            if t in owner:
                a, b = find(owner[t]), find(i)
                if a != b:
                    parent[max(a, b)] = min(a, b)
            else:
                owner[t] = i
    groups: dict[int, list] = {}
    for i, j in enumerate(judgements):
        groups.setdefault(find(i), []).append(j)
    out: list[GradedGarment] = []
    report: dict = {"judgements": len(judgements), "merged": [], "conflicts": [], "missing": []}
    for js in groups.values():
        first = js[0]
        grades = [j.taste for j in js]
        ids = [j.id for j in js]
        if len({g >= APPROVED for g in grades}) > 1:
            report["conflicts"].append({"judgement_ids": ids, "grades": grades})
            continue
        if len(js) > 1:
            report["merged"].append({"judgement_ids": ids, "grades": grades})
        pid = next((j.product_id for j in js if j.product_id in ident.live), None)
        if pid is None:
            for j in js:
                pids = ident.by_url.get(norm_url((j.snapshot or {}).get("source_url")) or "")
                if pids:
                    pid = min(pids)
                    break
        if pid is None:
            report["missing"].append({"judgement_ids": ids})
        family = ident.family(pid) if pid is not None else first.family_key
        out.append(GradedGarment(family, family, first.taste, pid, ids, grades, first.created_at, first.snapshot or {}))
    unit = ident.components([t.family for t in out])
    for t in out:
        t.unit = unit[t.family]
    out.sort(key=lambda t: (t.created_at, t.judgement_ids[0]))
    report["records"] = len(out)
    return out, report


@dataclass
class TestSet:
    """What an evaluation run measures on: graded garments, the seeds of everything held out with them (all graded
    listings, including grades left out as conflicting) and where the test set came from."""

    __test__ = False  # not a pytest class

    garments: list[GradedGarment]
    report: dict
    seeds: dict  # families, product_ids, urls: resolved by provenance.Identity.resolve
    source: dict  # {"kind": "unfrozen"} or the benchmark version, its hash and the training data (eval/benchmark.py)


def seeds_of(judgements) -> dict:
    return {
        "families": sorted({j.family_key for j in judgements}),
        "product_ids": sorted({j.product_id for j in judgements if j.product_id is not None}),
        "urls": sorted({u for j in judgements if (u := (j.snapshot or {}).get("source_url"))}),
    }


def unfrozen(db, ident) -> TestSet:
    """Your Evaluate grades as they are now (not a frozen benchmark: grades added, undone or changed since a freeze
    are all included as they stand)."""
    from app.models import Judgement

    judgements = db.query(Judgement).order_by(Judgement.id).all()
    garments, report = graded_garments(db, judgements, ident)
    return TestSet(garments, report, seeds_of(judgements), {"kind": "unfrozen"})


def held_out(ident, judgements):
    """Everything linked to these graded garments (all of them, including grades left out as conflicts)."""
    return ident.resolve(**seeds_of(judgements))


def purge_copy(db, held) -> dict:
    """Delete from an evaluation COPY every labelled record ``held`` covers: decisions (also about deleted listings
    at a held-out address), linked wardrobe pieces and matching inspiration photos. Returns what was deleted."""
    from app.models import Feedback, InspirationImage, WardrobeItem

    fb_ids = [fb.id for fb in db.query(Feedback) if held.covers_feedback(fb)]
    for i in range(0, len(fb_ids), 500):
        db.query(Feedback).filter(Feedback.id.in_(fb_ids[i : i + 500])).delete(synchronize_session=False)
    wardrobe = list(held.wardrobe_ids)
    if wardrobe:
        db.query(WardrobeItem).filter(WardrobeItem.id.in_(wardrobe)).delete(synchronize_session=False)
    inspiration = list(held.inspiration_ids)
    if inspiration:
        db.query(InspirationImage).filter(InspirationImage.id.in_(inspiration)).delete(synchronize_session=False)
    db.commit()
    return {"decisions": len(fb_ids), "wardrobe_pieces": len(wardrobe), "inspiration_photos": len(inspiration)}
