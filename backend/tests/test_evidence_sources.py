"""Evidence source identity: one post or page counts once however its link was copied, different posts count
separately, and a source cannot outvote itself.

SYNTHETIC links and claims, made up for these tests.
"""

from __future__ import annotations

import pytest

from app.models import Evidence
from app.services.quality import score_dimension
from app.services.sources import canonical_source

SAME = [
    # tracking parameters, fragments, host forms, letter case, trailing slashes
    ("https://forum.example/thread/42#post-9", "https://forum.example/thread/42"),
    ("HTTPS://Forum.Example/thread/42/", "http://forum.example/thread/42?fbclid=IwAR0abc&utm_source=x"),
    ("https://m.blog.example/review", "https://www.blog.example/review/?utm_campaign=spring&ref=feed"),
    ("blog.example/review", "https://blog.example/review"),
    # platform post ids: short links, mobile and old forms, title slugs, share parameters
    ("https://www.reddit.com/r/FashionReps/comments/abc123/qc_cashmere_coat/",
     "https://old.reddit.com/r/FashionReps/comments/abc123/"),
    ("https://reddit.com/r/FashionReps/comments/abc123/qc/?utm_source=share&utm_medium=ios_app", "https://redd.it/abc123"),
    ("https://reddit.com/r/x/comments/abc123/qc/def456/?context=3", "https://np.reddit.com/r/x/comments/abc123/other/def456"),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s&si=xyz", "https://youtu.be/dQw4w9WgXcQ?si=abc"),
    ("https://youtube.com/shorts/dQw4w9WgXcQ", "https://m.youtube.com/watch?v=dQw4w9WgXcQ&feature=share"),
    ("https://twitter.com/someone/status/1234567890?s=20&t=abc", "https://x.com/someone/status/1234567890"),
    ("https://www.instagram.com/p/Cxyz123/?igshid=abc", "https://instagram.com/reel/Cxyz123/"),
    ("https://item.taobao.com/item.htm?id=6123456789&spm=a1z10.1-c.w4004&ns=1",
     "https://m.intl.taobao.com/detail/detail.html?id=6123456789"),
    ("https://detail.tmall.com/item.htm?id=6123456789", "https://item.taobao.com/item.htm?id=6123456789"),
    ("https://weidian.com/item.html?itemID=4455&wfr=c&share_relation=x", "https://weidian.com/item.html?itemID=4455"),
    ("https://www.xiaohongshu.com/explore/64ab12?xsec_token=AB&xsec_source=pc_search",
     "https://www.xiaohongshu.com/discovery/item/64ab12"),
    ("https://shop.x.yupoo.com/albums/123456?uid=1&isSubCate=false", "https://shop.x.yupoo.com/albums/123456"),
]  # fmt: skip

DISTINCT = [
    ("https://reddit.com/r/FashionReps/comments/abc123/qc/", "https://reddit.com/r/FashionReps/comments/abc124/qc/"),
    # a comment is another person's report: not the post, and not another comment
    ("https://reddit.com/r/x/comments/abc123/qc/", "https://reddit.com/r/x/comments/abc123/qc/def456/"),
    ("https://reddit.com/r/x/comments/abc123/qc/def456/", "https://reddit.com/r/x/comments/abc123/qc/ghi789/"),
    # content-identifying parameters stay
    ("https://forum.example/viewtopic.php?t=10", "https://forum.example/viewtopic.php?t=11"),
    ("https://forum.example/viewtopic.php?t=10&p=5", "https://forum.example/viewtopic.php?t=10&p=6"),
    ("https://blog.example/review?page=1", "https://blog.example/review?page=2"),
    ("https://www.youtube.com/watch?v=aaaaaaaaaaa", "https://www.youtube.com/watch?v=bbbbbbbbbbb"),
    ("https://item.taobao.com/item.htm?id=1", "https://item.taobao.com/item.htm?id=2"),
    ("https://twitter.com/a/status/1", "https://twitter.com/a/status/2"),
    ("https://a.example/review", "https://b.example/review"),
    ("https://shop.x.yupoo.com/albums/1", "https://other.x.yupoo.com/albums/1"),
]


@pytest.mark.parametrize("a,b", SAME)
def test_equivalent_links_are_one_source(a, b):
    assert canonical_source(a) == canonical_source(b)


@pytest.mark.parametrize("a,b", DISTINCT)
def test_different_posts_and_pages_are_different_sources(a, b):
    assert canonical_source(a) != canonical_source(b)


def test_parameter_order_does_not_matter_but_values_do():
    assert canonical_source("https://f.example/v?t=1&p=2") == canonical_source("https://f.example/v?p=2&t=1")
    assert canonical_source(" ") is None and canonical_source(None) is None


def _ev(**kw):
    return Evidence(**{"aspect": "construction", "claim": "x", "strength": 1.0, "kind": "buyer_report", **kw})


def test_tracking_variants_of_one_review_count_once():
    url = "https://www.reddit.com/r/FashionReps/comments/abc123/qc_coat/"
    copies = [_ev(polarity=1, source_url=url + f"?utm_source=share&utm_content={i}") for i in range(4)]
    copies += [_ev(polarity=1, source_url="https://redd.it/abc123")]
    one = score_dimension([_ev(polarity=1, source_url=url)])
    many = score_dimension(copies)
    assert many["score"] == one["score"] and many["confidence"] == one["confidence"]
    assert many["sources"] == 1


def test_exact_duplicates_count_once():
    dup = [_ev(polarity=1, claim="Neat stitching", source_url="https://forum.example/t/9") for _ in range(3)]
    assert score_dimension(dup)["confidence"] == score_dimension(dup[:1])["confidence"]


def test_repeated_favourable_copies_cannot_outweigh_a_contrary_excerpt_from_the_same_source():
    url = "https://www.reddit.com/r/FashionReps/comments/abc123/qc_coat/"
    favourable = [_ev(polarity=1, claim="Stitching is neat", source_url=f"{url}?utm_source=copy{i}") for i in range(4)]
    contrary = _ev(polarity=-1, claim="Seams split after two wears", source_url=url)
    r = score_dimension([*favourable, contrary])
    assert r["sources"] == 1 and r["mixed_sources"] == 1
    assert r["score"] == 55 and r["label"] == "Mixed"  # evidence, but no direction: it says both
    # Before: the four copies outvoted the excerpt and the garment scored Good.
    alone = score_dimension(favourable)
    assert alone["score"] > 55 and alone["mixed_sources"] == 0


def test_a_contrary_excerpt_from_another_source_still_counts_against():
    good = [_ev(polarity=1, source_url=f"https://forum.example/t/{i}") for i in range(2)]
    bad = _ev(polarity=-1, source_url="https://forum.example/t/99")
    assert score_dimension([*good, bad])["score"] < score_dimension(good)["score"]


def test_one_post_attached_to_the_seller_and_to_two_of_its_products_counts_once(env, db):
    """Seller reputation reads seller-level evidence and its products' evidence together: one post filed three
    times, under three forms of its link, is one source."""
    from app.ingestion.base import RawListing
    from app.ingestion.pipeline import upsert_listing
    from app.models import Seller
    from app.services.quality import seller_reputation_status

    def seller_with(urls: list[str], slug: str):
        s = Seller(name=slug, slug=slug, platform="manual")
        db.add(s)
        db.flush()
        ps = [upsert_listing(db, s, RawListing(f"{slug}{i}", f"https://shop.example/{slug}{i}", f"coat {i}")).product
              for i in range(2)]  # fmt: skip
        db.add(Evidence(seller_id=s.id, kind="buyer_report", aspect="service", claim="Slow and rude", polarity=-1,
                        strength=0.5, source_url=urls[0]))  # fmt: skip
        for p, url in zip(ps, urls[1:], strict=True):
            db.add(Evidence(product_id=p.id, kind="buyer_report", aspect="defect", claim="Slow and rude",
                            polarity=-1, strength=0.5, source_url=url))  # fmt: skip
        db.flush()
        return s

    one_post = seller_with(["https://redd.it/zz9", "https://www.reddit.com/r/x/comments/zz9/slug/?utm_source=share",
                            "https://old.reddit.com/r/x/comments/zz9/"], "onepost")  # fmt: skip
    three_posts = seller_with([f"https://reddit.com/r/x/comments/zz{i}/" for i in range(3)], "threeposts")
    single = seller_with(["https://redd.it/yy1", "https://redd.it/yy1", "https://redd.it/yy1"], "single")
    # One moderately specific report is not enough to judge a seller; three separate ones are.
    assert seller_reputation_status(db, single) == "insufficient evidence"
    assert seller_reputation_status(db, one_post) == "insufficient evidence"
    assert seller_reputation_status(db, three_posts) == "concerns"
