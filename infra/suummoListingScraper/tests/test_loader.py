"""
Offline tests for the pure parts of load_listings.py (no database needed).
The database behaviour (upsert, rollback, delisting) is checked by running the
loader and the psql queries shown in the step instructions.
"""
import pathlib

import pytest

from kansai_scraper.extract import extract_listing
from load_listings import LISTING_COLUMNS, dedupe_stations, to_row

FIXTURES = pathlib.Path(__file__).parent / "fixtures"


def scraped(n):
    html = (FIXTURES / f"listing_{n}_trimmed.html").read_text(encoding="utf-8")
    return extract_listing(html, "https://suumo.jp/chintai/x/")


def test_to_row_maps_listing_id_to_external_id():
    row = to_row(scraped(1))
    assert row["external_id"] == "jnc_000109954940"
    assert row["rent_yen"] == 97000
    assert row["requires_shikikin"] is True and row["requires_reikin"] is False


def test_to_row_covers_every_column_except_agency_id():
    # agency_id is filled in by the loader after the agency upsert
    row = to_row(scraped(2))
    assert set(row) == set(LISTING_COLUMNS) - {"agency_id"}


def test_to_row_rejects_listings_outside_kansai():
    item = scraped(1)
    item["prefecture"] = item["city"] = None
    with pytest.raises(ValueError, match="outside Kansai"):
        to_row(item)


def test_to_row_never_passes_none_for_json_costs():
    item = scraped(1)
    item["other_initial_costs"] = None
    assert to_row(item)["other_initial_costs"] == {}


def test_dedupe_stations_keeps_shortest_walk_per_station_name():
    stations = [
        {"line": "JR線", "station": "天満駅", "walk_minutes": 10},
        {"line": "地下鉄線", "station": "天満駅", "walk_minutes": 7},
        {"line": "JR線", "station": "桜ノ宮駅", "walk_minutes": 8},
    ]
    result = {s["station"]: s["walk_minutes"] for s in dedupe_stations(stations)}
    assert result == {"天満駅": 7, "桜ノ宮駅": 8}
