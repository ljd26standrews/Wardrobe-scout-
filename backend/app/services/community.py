"""Community research leads (optional).

Uses Reddit's public JSON search endpoint (read-only, rate-limited, no login).
Results are stored as *leads* - you decide which become evidence, and with
what polarity, because relevance and honesty of posts can't be assumed.
Affiliate/promotional markers are flagged automatically.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from urllib.parse import quote_plus

from sqlalchemy.orm import Session

from ..ingestion.http import FetchError, PoliteClient
from ..models import CommunityLead
from .settings_store import get_setting

AFFILIATE_MARKERS = re.compile(
    r"affiliate|discount code|promo code|coupon|use my link|referral|invite code|邀请码|sponsored|"
    r"\bad\b|#ad|partnered|sign ?up bonus|my link|\bcode:?\s*[A-Z0-9]{4,}",
    re.I,
)


def looks_affiliate(text: str) -> bool:
    return bool(AFFILIATE_MARKERS.search(text or ""))


def parse_reddit_search(data: dict) -> list[dict]:
    out = []
    for child in (data.get("data") or {}).get("children", []):
        d = child.get("data") or {}
        if not d.get("permalink"):
            continue
        text = f"{d.get('title', '')}\n{d.get('selftext', '')}"
        out.append(
            {
                "url": "https://www.reddit.com" + d["permalink"],
                "title": d.get("title", ""),
                "excerpt": (d.get("selftext") or "")[:800] or None,
                "author": d.get("author"),
                "posted_at": datetime.fromtimestamp(d.get("created_utc", 0), UTC).replace(tzinfo=None)
                if d.get("created_utc")
                else None,
                "subreddit": d.get("subreddit"),
                "possible_affiliate": looks_affiliate(text),
            }
        )
    return out


def search_reddit(
    db: Session,
    query: str,
    *,
    seller_id: int | None = None,
    product_id: int | None = None,
    client: PoliteClient | None = None,
) -> dict:
    cfg = get_setting(db, "community")
    subs = cfg.get("subreddits") or []
    own = client is None
    client = client or PoliteClient(delay=2.0)
    found, new = 0, 0
    errors = []
    try:
        targets = [
            f"https://www.reddit.com/r/{s}/search.json?q={quote_plus(query)}&restrict_sr=1&sort=new&limit=25"
            for s in subs
        ] or [f"https://www.reddit.com/search.json?q={quote_plus(query)}&sort=new&limit=25"]
        for url in targets:
            try:
                r = client.get(url)
                items = parse_reddit_search(r.json())
            except (FetchError, ValueError) as e:
                errors.append(str(e))
                continue
            for it in items:
                found += 1
                if db.query(CommunityLead).filter_by(url=it["url"]).first():
                    continue
                db.add(
                    CommunityLead(
                        seller_id=seller_id,
                        product_id=product_id,
                        source=f"reddit/{it['subreddit']}",
                        url=it["url"],
                        title=it["title"],
                        excerpt=it["excerpt"],
                        author=it["author"],
                        posted_at=it["posted_at"],
                        possible_affiliate=it["possible_affiliate"],
                    )
                )
                new += 1
        db.commit()
    finally:
        if own:
            client.close()
    return {"found": found, "new": new, "errors": errors}
