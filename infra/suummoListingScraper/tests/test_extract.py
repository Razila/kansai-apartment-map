"""
Offline tests for kansai_scraper/extract.py. No network, no Scrapy, no database.

Run from the scraper project root (the folder containing scrapy.cfg):
    python -m pytest tests -v

Why these tests matter: the scraper depends on SUUMO's page markup, which SUUMO
can change at any time. If extraction breaks, THESE tests fail (once you refresh
the fixtures from a new page) instead of bad data silently reaching the database.
"""
import pathlib

import pytest

from kansai_scraper.extract import classify_stove, extract_listing, parse_stations, parse_yen, split_address

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def load(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


# --- the two real listings, covering BOTH 敷金/礼金 cases -------------------------

def test_listing_1_deposit_required_key_money_not():
    r = extract_listing(load("listing_1_trimmed.html"), "https://suumo.jp/chintai/jnc_000109954940/?bc=100527833746")
    assert r["listing_id"] == "jnc_000109954940"
    assert r["suumo_code"] == "100527833746"
    assert r["title_ja"] == "セレニテ天六ミラク"
    assert (r["prefecture"], r["city"], r["ward"]) == ("大阪府", "大阪市", "北区")
    assert r["rent_yen"] == 97000
    assert r["management_fee_yen"] == 7200
    # the hover-card rule: 敷金 bold (required), 礼金 hollow (not required)
    assert r["requires_shikikin"] is True
    assert r["requires_reikin"] is False
    assert r["shikikin_months"] == 1.0
    assert r["room_layout"] == "1DK"
    assert r["size_sqm"] == 23.25
    assert r["built_year"] == 2024
    assert r["nearest_station_walk_min"] == 2
    assert len(r["stations"]) == 3
    assert r["stove_type"] == "IH"
    assert r["fixed_term_lease"] is False        # 普通借家 2年
    assert r["pets_negotiable"] is False
    assert r["agency_name_ja"].startswith("AFLO")
    assert r["thumbnail_url"].endswith("100527833746_go.jpg")


def test_listing_2_key_money_required_deposit_not():
    r = extract_listing(load("listing_2_trimmed.html"), "https://suumo.jp/chintai/jnc_000109385558/?bc=100525652452")
    assert r["listing_id"] == "jnc_000109385558"
    assert r["rent_yen"] == 124000
    assert r["requires_shikikin"] is False
    assert r["requires_reikin"] is True
    assert r["reikin_yen"] == 248000
    assert r["reikin_months"] == 2.0
    assert r["parking_available"] is False
    assert r["other_initial_costs"]["ほか初期費用"].startswith("合計2.75万円")
    assert r["has_ac"] and r["secured_entrance"] and r["has_washer_hookup"]
    assert r["stove_type"] == "stove"            # 2口コンロ (+ ガスコンロ対応)


# Guard against over-trimming: if the untrimmed pages are present, results must match.
@pytest.mark.skipif(not (FIXTURES / "detail_sample_1.html").exists(), reason="full page not present")
@pytest.mark.parametrize("n", [1, 2])
def test_trimmed_fixture_matches_full_page(n):
    url = "https://suumo.jp/chintai/x/"
    assert extract_listing(load(f"listing_{n}_trimmed.html"), url) == extract_listing(load(f"detail_sample_{n}.html"), url)


def test_missing_required_field_fails_loudly():
    with pytest.raises(ValueError):
        extract_listing("<html><body><h1>no data here</h1></body></html>", "https://suumo.jp/x")


# --- the small parsing helpers -------------------------------------------------

@pytest.mark.parametrize("text,expected", [
    ("9.7万円", 97000), ("12.4万円", 124000), ("24.8万円", 248000),
    ("7200円", 7200), ("12,000円", 12000), ("-", None), ("", None), (None, None),
])
def test_parse_yen(text, expected):
    assert parse_yen(text) == expected


@pytest.mark.parametrize("address,expected", [
    ("大阪府大阪市北区長柄中１", ("大阪府", "大阪市", "北区")),
    ("京都府京都市下京区四条通", ("京都府", "京都市", "下京区")),
    ("兵庫県神戸市中央区三宮町1", ("兵庫県", "神戸市", "中央区")),
    ("大阪府堺市堺区南瓦町", ("大阪府", "堺市", "堺区")),
    ("大阪府吹田市江坂町1-2", ("大阪府", "吹田市", None)),       # no ward
    ("滋賀県大津市浜大津1", ("滋賀県", "大津市", None)),
    ("奈良県生駒郡三郷町立野南", ("奈良県", "生駒郡三郷町", None)),  # 郡 + 町
    ("東京都新宿区西新宿", (None, None, None)),                   # outside Kansai
])
def test_split_address(address, expected):
    assert split_address(address) == expected


def test_parse_stations():
    text = "地下鉄谷町線/天神橋筋六丁目駅 歩2分 ＪＲ大阪環状線/天満駅 歩10分"
    assert parse_stations(text) == [
        {"line": "地下鉄谷町線", "station": "天神橋筋六丁目駅", "walk_minutes": 2},
        {"line": "ＪＲ大阪環状線", "station": "天満駅", "walk_minutes": 10},
    ]


# --- stove classification --------------------------------------------------------

@pytest.mark.parametrize("equipment,expected", [
    ("エアコン、2口コンロ、IHクッキングヒーター、駐輪場", "IH"),     # real sample 1
    ("エアコン、ガスコンロ対応、2口コンロ、駐輪場", "stove"),        # real sample 2
    ("エアコン、ガスコンロ対応、駐輪場", "gas_ready"),               # bring your own stove
    ("エアコン、コンロ設置可", "gas_ready"),
    ("エアコン、システムキッチン", None),
])
def test_classify_stove(equipment, expected):
    assert classify_stove(equipment) == expected


# --- pets and lease type ----------------------------------------------------------
# No real page we have contains these terms in the positive case, so these tests
# inject the term into a real fixture. They prove the RULE works; they do not prove
# SUUMO puts the term in that spot. Replace with a real page once we capture one.

def _inject(name, old, new):
    html = load(name)
    assert old in html, f"test setup: {old!r} not found in {name}"
    return html.replace(old, new, 1)


def test_pets_negotiable_detected_in_equipment_list():
    html = _inject("listing_1_trimmed.html", "バストイレ別、", "バストイレ別、ペット相談、")
    assert extract_listing(html, "https://suumo.jp/chintai/x/")["pets_negotiable"] is True


def test_fixed_term_lease_detected():
    html = _inject("listing_1_trimmed.html", "普通借家", "定期借家")
    assert extract_listing(html, "https://suumo.jp/chintai/x/")["fixed_term_lease"] is True


# --- real ペット相談 page (local only: the full page is git-ignored, so CI skips this) ---
@pytest.mark.skipif(not (FIXTURES / "pets_sample.html").exists(), reason="full page not present")
def test_real_pets_page():
    r = extract_listing(load("pets_sample.html"), "https://suumo.jp/chintai/jnc_000107255542/?bc=1")
    assert r["pets_negotiable"] is True
    assert (r["rent_yen"], r["management_fee_yen"]) == (131000, 15000)
    assert (r["requires_shikikin"], r["requires_reikin"]) == (False, False)
    assert (r["floor"], r["total_floors"]) == (6, 11)
    assert r["parking_available"] is True and r["parking_text"] == "敷地内41800円"
    assert r["stove_type"] == "stove"        # 2口コンロ = installed 2-burner stove
    assert r["fixed_term_lease"] is False
    assert r["nearest_station_walk_min"] == 8 and len(r["stations"]) == 3
