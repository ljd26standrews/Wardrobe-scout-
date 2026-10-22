"""Which records are about the same garment: keeping held-out labels out of training (evaluation, out-of-fold
calibration) and counting each garment once.

Identity: one garment is
* one family of listings (``Product.group_id``: listings you confirmed as the same, a shared purchase link, or two or
  more shared photographs), as approval labels already count it;
* a listing and any listing or since-deleted listing at the same address (a re-import under another id; a decision
  about a deleted listing keeps its address in its snapshot).

Held-out exclusion is wider, so that a later match or a re-import cannot open a path for a held-out label into
training. It starts from what was held out (a family key as recorded when the label was given, as it is now, a
listing, an address) and covers
* the listing named by a recorded family key, and every current member of that family (a family's key is its
  lowest listing id, so a recorded "g<id>" names a listing that was in it);
* every listing connected through a match signal that can merge it into the family later: the strong relations and
  a single shared photograph, transitively (a pair you said are different is no link, though each may still be
  reached through others: exclusion errs towards leaving out); a visually similar design is a different garment by
  the matching rules and stays;
* every listing, deleted listing and wardrobe piece at the same address.

Nothing labelled that is linked to a held-out garment teaches a model being evaluated on it:
* decisions about any of those listings, and decisions about deleted listings at those addresses;
* wardrobe pieces linked to one of them (by listing or address), whatever their rating;
* inspiration photos that are one of their photographs (perceptual hash within the photo-matching distance).

Unlabelled context stays, as when a candidate is served: the catalogue (feature vocabulary, photo components), your
written profile, and the background sample of undecided listings, in which a held-out listing is undecided.

Where provenance cannot establish identity it is reported (``HeldOut.unlinked``), not guessed: inspiration photos
that match no listing photo, wardrobe pieces with neither a listing nor an address, decisions about deleted listings
recorded without an address.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from ..models import Feedback, InspirationImage, Product, ProductImage, ProductRelation, WardrobeItem
from .dedup import PHASH_MAX_DISTANCE, STRONG_RELATIONS

# Match signals under which two listings are, or may later become, one family.
ALIAS_RELATIONS = frozenset(STRONG_RELATIONS | {"similar_photo"})


def norm_url(url: str | None) -> str | None:
    """An address as compared for identity: scheme, the letter case of the host, a trailing slash and a fragment do
    not matter."""
    if not url or not url.strip():
        return None
    raw = url.strip()
    try:
        s = urlsplit(raw)
        host = (s.hostname or "").lower()
        port = f":{s.port}" if s.port else ""
    except ValueError:
        return raw
    if not host:
        return raw
    return f"//{host}{port}{s.path.rstrip('/')}" + (f"?{s.query}" if s.query else "")


def product_family(pid: int, group_id: int | None) -> str:
    return f"g{group_id}" if group_id is not None else f"p{pid}"


def live_families_by_url(db: Session, urls: Iterable[str | None]) -> dict[str, str]:
    """For addresses (as recorded), the family of a listing that exists now at that address: a decision about a
    deleted listing belongs to the same garment as the listing re-imported at its address. One query, only when
    there are addresses to look up."""
    wanted = {u for u in (norm_url(x) for x in urls) if u}
    if not wanted:
        return {}
    out: dict[str, str] = {}
    rows = db.query(Product.id, Product.group_id, Product.source_url).order_by(Product.id)
    for pid, gid, url in rows:
        u = norm_url(url)
        if u in wanted and u not in out:
            out[u] = product_family(pid, gid)
    return out


@dataclass(frozen=True)
class HeldOut:
    """What a held-out set covers (see the module docstring). Empty: nothing is held out."""

    families: frozenset[str] = frozenset()
    product_ids: frozenset[int] = frozenset()
    urls: frozenset[str] = frozenset()
    wardrobe_ids: frozenset[int] = frozenset()
    inspiration_ids: frozenset[int] = frozenset()
    unlinked: dict = field(default_factory=dict, compare=False)

    def __bool__(self) -> bool:
        return bool(self.families or self.product_ids or self.urls or self.wardrobe_ids or self.inspiration_ids)

    def covers_feedback(self, fb: Feedback) -> bool:
        if fb.product_id is not None and fb.product_id in self.product_ids:
            return True
        url = (fb.snapshot or {}).get("source_url")
        if fb.product_id is None and url and f"d{url}" in self.families:
            return True
        return norm_url(url) in self.urls if url else False

    def covers_outcome(self, o) -> bool:
        """A rated outcome (approval_data.RatedOutcome) about a held-out garment."""
        if o.family in self.families or (o.product_id is not None and o.product_id in self.product_ids):
            return True
        url = (o.snapshot or {}).get("source_url")
        return norm_url(url) in self.urls if url else False

    def summary(self) -> dict:
        return {"families": len(self.families), "listings": len(self.product_ids), "addresses": len(self.urls),
                "wardrobe_pieces": len(self.wardrobe_ids), "inspiration_photos": len(self.inspiration_ids),
                "unlinked": dict(self.unlinked)}  # fmt: skip


class Identity:
    """The identity links of one database state, read once and used for many held-out sets (calibration folds,
    rolling-origin blocks, an evaluation's test set)."""

    def __init__(self, db: Session):
        self.db = db
        rows = db.query(Product.id, Product.group_id, Product.source_url).all()
        self.live = {pid for pid, _, _ in rows}
        self.group_of = {pid: gid for pid, gid, _ in rows if gid is not None}
        self.members: dict[int, set[int]] = defaultdict(set)
        for pid, gid in self.group_of.items():
            self.members[gid].add(pid)
        self.url_of = {pid: u for pid, _, url in rows if (u := norm_url(url))}
        self.by_url: dict[str, set[int]] = defaultdict(set)
        for pid, u in self.url_of.items():
            self.by_url[u].add(pid)
        self.links: dict[int, set[int]] = defaultdict(set)
        rels = db.query(ProductRelation.a_id, ProductRelation.b_id).filter(
            ProductRelation.relation.in_(ALIAS_RELATIONS), ProductRelation.user_confirmed.isnot(False)
        )
        for a, b in rels:
            self.links[a].add(b)
            self.links[b].add(a)
        self._wardrobe: list[tuple[int, int | None, str | None]] | None = None
        self._inspiration: list[tuple[int, int]] | None = None
        self._unlinked: dict | None = None

    def family(self, pid: int) -> str:
        return product_family(pid, self.group_of.get(pid))

    def family_of_url(self, url: str | None) -> str | None:
        """The family of a listing at this address now (lowest id first), or None."""
        pids = self.by_url.get(norm_url(url) or "")
        return self.family(min(pids)) if pids else None

    def _seed_products(self, families: Iterable[str]) -> tuple[set[int], set[str]]:
        pids: set[int] = set()
        urls: set[str] = set()
        for key in families:
            kind, value = key[:1], key[1:]
            if kind in ("g", "p") and value.isdigit():
                n = int(value)
                if n in self.live:
                    pids.add(n)
                if kind == "g":
                    pids |= self.members.get(n, set())
            elif kind == "d" and (u := norm_url(value)):
                urls.add(u)
        return pids, urls

    def closure(self, pids: Iterable[int], urls: Iterable[str] = ()) -> tuple[set[int], set[str]]:
        """Every listing (and address) the given listings and addresses are, or may become, one garment with."""
        seen: set[int] = set()
        addresses = set(urls)
        todo = [p for p in pids if p in self.live]
        for u in addresses:
            todo += self.by_url.get(u, ())
        while todo:
            p = todo.pop()
            if p in seen:
                continue
            seen.add(p)
            nxt = set(self.links.get(p, ()))
            if p in self.group_of:
                nxt |= self.members[self.group_of[p]]
            if (u := self.url_of.get(p)) is not None:
                addresses.add(u)
                nxt |= self.by_url[u]
            todo += [q for q in nxt if q not in seen]
        return seen, addresses

    def resolve(
        self, *, families: Iterable[str] = (), product_ids: Iterable[int] = (), urls: Iterable[str | None] = ()
    ) -> HeldOut:
        """The held-out set for these family keys (recorded or current), listings and addresses."""
        families = set(families)
        seeds, seed_urls = self._seed_products(families)
        seeds |= {p for p in product_ids if p is not None}
        seed_urls |= {u for u in (norm_url(x) for x in urls) if u}
        pids, addresses = self.closure(seeds, seed_urls)
        families |= {self.family(p) for p in pids}
        wardrobe = frozenset(w for w, pid, u in self._wardrobe_rows() if pid in pids or (u and u in addresses))
        inspiration = self._matching_inspiration(pids)
        return HeldOut(frozenset(families), frozenset(pids), frozenset(addresses), wardrobe, inspiration,
                       self.unlinked())  # fmt: skip

    def components(self, families: Iterable[str]) -> dict[str, str]:
        """For each family, the unit a split must keep together: families that are, or may become, one garment
        (connected as in ``closure``) share one, named by the smallest family key among them."""
        families = sorted(set(families))
        parent = {f: f for f in families}

        def find(f: str) -> str:
            while parent[f] != f:
                parent[f] = parent[parent[f]]
                f = parent[f]
            return f

        owner: dict[object, str] = {}
        for f in families:
            pids, urls = self._seed_products([f])
            pids, urls = self.closure(pids, urls)
            for token in [*pids, *(("u", u) for u in urls)]:
                if token in owner:
                    a, b = find(owner[token]), find(f)
                    if a != b:
                        parent[max(a, b)] = min(a, b)
                else:
                    owner[token] = f
        return {f: find(f) for f in families}

    # ------------------------------------------------------------- non-listing evidence
    def _wardrobe_rows(self) -> list[tuple[int, int | None, str | None]]:
        if self._wardrobe is None:
            rows = self.db.query(WardrobeItem.id, WardrobeItem.product_id, WardrobeItem.source_url)
            self._wardrobe = [(i, pid, norm_url(url)) for i, pid, url in rows]
        return self._wardrobe

    def _inspiration_rows(self) -> list[tuple[int, int]]:
        if self._inspiration is None:
            rows = self.db.query(InspirationImage.id, InspirationImage.phash).filter(InspirationImage.phash.isnot(None))
            self._inspiration = [(i, h) for i, ph in rows if (h := _hash_int(ph)) is not None]
        return self._inspiration

    def _matching_inspiration(self, pids: set[int]) -> frozenset[int]:
        insp = self._inspiration_rows()
        if not insp or not pids:
            return frozenset()
        hashes: list[int] = []
        ids = sorted(pids)
        for i in range(0, len(ids), 500):
            q = self.db.query(ProductImage.phash).filter(ProductImage.product_id.in_(ids[i : i + 500]),
                                                         ProductImage.phash.isnot(None))  # fmt: skip
            hashes += [h for (ph,) in q if (h := _hash_int(ph)) is not None]
        return frozenset(i for i, h in insp if any(bin(h ^ x).count("1") <= PHASH_MAX_DISTANCE for x in hashes))

    def unlinked(self) -> dict:
        """Evidence whose identity provenance cannot establish (so no held-out set can cover it), by kind."""
        if self._unlinked is not None:
            return self._unlinked
        no_link = sum(1 for _, pid, u in self._wardrobe_rows() if pid is None and not u)
        insp_total = self.db.query(InspirationImage.id).count()
        no_hash = insp_total - len(self._inspiration_rows())
        orphans = self.db.query(Feedback.snapshot).filter(Feedback.product_id.is_(None))
        no_address = sum(1 for (snap,) in orphans if snap and not (snap or {}).get("source_url"))
        self._unlinked = {"wardrobe_without_listing_or_address": no_link,
                          "inspiration_photos_unhashed": no_hash,
                          "inspiration_photos_total": insp_total,  # linked only when they are a listing's photo
                          "deleted_listing_decisions_without_address": no_address}  # fmt: skip
        return self._unlinked


def _hash_int(h: str | None) -> int | None:
    try:
        return int(h, 16) if h else None
    except ValueError:
        return None
