"""Regression tests for defects found in the phase-1 audit (each failed before the fix)."""

from app.ingestion.parsing import parse_category, parse_item_code, parse_listing_text, parse_materials, parse_price
from app.ingestion.translate import glossary_translate


def test_P2_retail_price_listed_first():
    # Sellers commonly quote boutique retail price before their own price
    assert parse_price("Loro Piana 羊绒开衫 专柜价¥12800 现价¥899") == 899


def test_P3_year_is_not_item_code():
    assert parse_item_code("2024秋冬新款 羊绒大衣") is None


def test_P4_imitation_cashmere_is_not_cashmere():
    fibres = [m["fibre"] for m in parse_materials("仿羊绒手感 毛衣 100%腈纶")]
    assert "cashmere" not in fibres, fibres


def test_P4b_cashmere_feel_english():
    fibres = [m["fibre"] for m in parse_materials("cashmere-feel knit, 100% acrylic")]
    assert "cashmere" not in fibres, fibres


def test_P5_hooded_jacket_is_jacket_not_hoodie():
    assert parse_category("连帽夹克 防风") in ("jacket", "zip jacket")


def test_P6_padded_cotton_coat_translation():
    out = glossary_translate("棉服 外套")
    assert "服" not in out, out


def test_P7_price_range_first_value_is_lowest():
    # "¥399-459" (size-dependent price) should not be reported as the higher bound
    assert parse_price("羊毛裤 ¥399-459") == 399


def test_P8_premium_rules_bonus_from_fake_cashmere_claim():
    a = parse_listing_text("仿羊绒 圆领毛衣 黑色")
    assert all(m["fibre"] != "cashmere" for m in a.materials)


def test_price_retail_only_is_unknown_not_wrong():
    assert parse_price("Loro Piana 羊绒开衫 专柜价¥12800") is None


def test_price_selling_label_wins_regardless_of_order():
    assert parse_price("售价¥899 专柜¥12800") == 899
    assert parse_price("RRP ¥8600, now ¥760") == 760


def test_cashmere_like_hand_feel_and_faux():
    assert parse_materials("羊绒手感 针织衫") == []
    assert [m["fibre"] for m in parse_materials("faux cashmere jumper")] == []
    assert [m["fibre"] for m in parse_materials("70%羊毛 30%羊绒 羊绒手感")] == ["wool", "cashmere"]
