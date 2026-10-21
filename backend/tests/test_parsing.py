from app.ingestion.parsing import (
    has_cjk,
    parse_brand,
    parse_category,
    parse_colours,
    parse_gsm,
    parse_item_code,
    parse_listing_text,
    parse_logo_level,
    parse_materials,
    parse_price,
    parse_sizes,
    weidian_item_id,
)
from app.ingestion.translate import glossary_translate


def test_price_formats():
    assert parse_price("【8009】Acne 卫衣 ¥919") == 919
    assert parse_price("P399 初剪羊毛休闲裤") == 399
    assert parse_price("价格：410元") == 410
    assert parse_price("Size 48 wool") is None  # no currency marker => no guess


def test_item_code():
    assert parse_item_code("【8009】Acne hoodie") == "8009"
    assert parse_item_code("货号: A1234 夹克") == "A1234"
    assert parse_item_code("Plain hoodie") is None


def test_materials_percentages_cn_and_en():
    m = parse_materials("成分：70%羊毛 30%羊绒")
    assert m == [{"fibre": "wool", "pct": 70.0}, {"fibre": "cashmere", "pct": 30.0}]
    m = parse_materials("Composition: cotton 95%, elastane 5%")
    assert {"fibre": "cotton", "pct": 95.0} in m and {"fibre": "elastane", "pct": 5.0} in m


def test_materials_without_percent_and_specific_wool():
    m = parse_materials("100%初剪羊毛 休闲裤")
    assert m[0]["fibre"] == "virgin wool"
    assert parse_materials("纯棉T恤") == [{"fibre": "cotton", "pct": 100.0}]
    fibres = [x["fibre"] for x in parse_materials("merino wool knit")]
    assert fibres == ["merino wool"]  # generic "wool" suppressed


def test_materials_implausible_total_drops_percentages():
    m = parse_materials("80%羊毛 70%羊绒")
    assert all(x["pct"] is None for x in m)


def test_gsm():
    assert parse_gsm("260g重磅纯棉") == 260
    assert parse_gsm("克重：320") == 320
    assert parse_gsm("weight 2kg") is None


def test_sizes():
    assert parse_sizes("尺码：S-XL") == ["S", "M", "L", "XL"]
    assert parse_sizes("尺码 46 48 50 52") == ["46", "48", "50", "52"]
    assert parse_sizes("size: M L XL") == ["M", "L", "XL"]
    assert parse_sizes("It's an S-shaped seam") == []


def test_colours_specific_before_generic():
    assert parse_colours("深灰 宽松") == ["charcoal"]
    assert parse_colours("black and cream") == ["cream", "black"]
    assert "navy" in parse_colours("藏青色") and "blue" not in parse_colours("藏青色")


def test_category_and_brand():
    assert parse_category("Acne 羊毛羊绒连帽卫衣") == "hoodie"
    assert parse_category("初剪羊毛休闲裤") == "trousers"
    assert parse_category("双头拉链夹克") == "zip jacket"
    assert parse_category("260g重磅纯棉短袖T恤") == "t-shirt"
    assert parse_brand("LP 羊绒开衫") == "Loro Piana"
    assert parse_brand("loro piana style trousers") == "Loro Piana"
    assert parse_brand("Acne Studios hoodie") == "Acne Studios"
    assert parse_brand("help me") is None  # "lp" inside words must not match


def test_logo_level():
    assert parse_logo_level("无logo 纯色") == "none"
    assert parse_logo_level("大logo印花") == "visible"
    assert parse_logo_level("胸前小标") == "subtle"
    assert parse_logo_level("plain description") == "none"
    assert parse_logo_level("knit sweater") == "unknown"


def test_full_listing_and_links():
    a = parse_listing_text(
        "【8009】Acne 羊毛羊绒连帽卫衣 ¥919",
        "成分：70%羊毛 30%羊绒\n尺码：S-XL\nhttps://weidian.com/item.html?itemID=7707075268",
    )
    assert a.price == 919 and a.item_code == "8009" and a.category == "hoodie"
    assert a.links["weidian"].endswith("7707075268")
    assert set(a.seasons) == {"autumn", "winter"}
    assert weidian_item_id(a.links["weidian"]) == "7707075268"


def test_glossary_translation_keeps_unknown_text():
    out = glossary_translate("初剪羊毛休闲裤 深灰")
    assert "virgin wool" in out and "casual trousers" in out and "charcoal" in out
    assert not has_cjk(glossary_translate("羊绒开衫"))
    assert "某" in glossary_translate("某某羊绒")  # unknown characters are preserved, not invented
