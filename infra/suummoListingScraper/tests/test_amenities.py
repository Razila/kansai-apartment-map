"""
Offline tests for fetch_amenities.py -- no internet, no database.

HOW THESE TESTS WORK
    Same technique as test_geocode.py: the network call (`post`) and the waiting
    (`sleep`) are injectable, so we hand the code fakes. The Overpass answers below are
    small hand-written examples in the real response SHAPE (the shape is documented at
    https://wiki.openstreetmap.org/wiki/Overpass_API). They contain no scraped listing
    data, so they are safe to keep in the public repo.

WHAT THESE TESTS DO NOT COVER
    * That the real Overpass server accepts our query and answers in this shape ->
      the manual test in the step instructions (run it for one category and read the count).
    * The SQL -> tests/test_amenity_sql.py (needs a PostGIS database; skipped otherwise).

Run from infra/suummoListingScraper:   python -m pytest tests/test_amenities.py -v
"""
import pytest

from fetch_amenities import (
    AMENITY_FILTERS, AMENITY_TYPES, RETRY_DELAYS_SECONDS, bbox_with_margin,
    build_query, download, parse_elements,
)


def test_every_category_has_a_filter_and_the_list_matches():
    """AMENITY_TYPES is derived from AMENITY_FILTERS, so the two can never disagree.

    Protects against: adding a category to one place and forgetting the other, which
    would make `--types` accept a name that then crashes with a KeyError mid-run.
    """
    assert set(AMENITY_TYPES) == set(AMENITY_FILTERS)
    assert {"supermarket", "convenience_store", "ramen", "bus_stop", "school"} == set(AMENITY_TYPES)


def test_build_query_contains_filter_bbox_and_center_output():
     """The query must include our filter, the rectangle in (S,W,N,E) order, and `out center`.

    Protects against: swapped bbox order (Overpass would silently search the wrong part
    of the world and return nothing) and a missing `center` (buildings/ways would come
    back without coordinates and be dropped by parse_elements).
    """
     q = build_query("supermarket", (34.5, 135.1, 35.1, 135.9))
     assert 'nwr["shop"="supermarket"](34.5,135.1,35.1,135.9)' in q
     assert "out center tags;" in q
     assert "[out:json]" in q


def test_ramen_filter_matches_whole_cuisine_items_only():
    """The ramen regex must match 'ramen' as one whole item in a ';'-separated list.

    The filter text is what Overpass evaluates, so here we check the regex itself with
    Python's `re`, which understands the same pattern. 'ramen' and 'ramen;noodle' must
    match; 'unramenable' (a word merely CONTAINING ramen) must not.
    Protects against: loosening the regex to plain "ramen", which would match unrelated
    cuisines that happen to contain the word.
    """ 
    import re
    pattern = re.search(r'"cuisine"~"([^"]+)"', AMENITY_FILTERS["ramen"]).group(1)
    assert re.search(pattern, "ramen")
    assert re.search(pattern, "japanese;ramen;noodle")
    assert not re.search(pattern, "unramenable")


def test_bbox_with_margin_grows_every_side_in_swne_order():
    """Margin is applied outward on all four sides and returned as (S, W, N, E)."""
    assert bbox_with_margin(34.6, 135.2, 35.0, 135.8, 0.03) == (34.57, 135.17, 35.03, 135.83)


def test_parse_elements_handles_nodes_ways_names_and_missing_coordinates():
    """One payload exercising every branch of parse_elements.

    Elements, in order:
      1. a node with a Japanese name        -> kept, coordinates read from the element
      2. a way (building) with only a plain name -> kept, coordinates read from `center`
      3. a node with no name at all          -> kept, name None (bus stops are often unnamed)
      4. an element with no coordinates       -> skipped, must not crash
    Also checks: name:ja is preferred over name, and tags are filtered to the whitelist
    (the 'wheelchair' tag must be dropped).
    """
    payload = {"elements": [
        {"type": "node", "id": 1, "lat": 34.70, "lon": 135.50,
         "tags": {"name": "Life", "name:ja": "ライブ", "brand": "Life", "wheelchair": "yes"}},
        {"type": "way", "id": 2, "center": {"lat": 34.71, "lon": 135.51},
         "tags": {"name": "Izumiya"}},
        {"type": "node", "id": 3, "lat": 34.72, "lon": 135.52, "tags": {}},
        {"type": "way", "id": 4, "tags": {"name": "no location"}},

    ]}
    rows = parse_elements(payload, "supermarket")

    assert [r["osm_id"] for r in rows] == [1, 2, 3]     # element 4 skipped
    assert rows[0]["name"] == "ライブ"                  # name:ja wins over name
    assert rows[0]["tags"] == {"name": "Life", "name:ja":"ライブ", "brand": "Life"} # wheelchair dropped
    assert (rows[1]["lat"], rows[1]["lon"]) == (34.71, 135.51)  # way -> centre point
    assert rows[2]["name"] is None # unnamed stays None
    assert all(r["amenity_type"] == "supermarket" for r in rows)


def test_parse_elements_tolerates_empty_answer():
    """An answer with no elements (nothing of that type in the area) is an empty list."""
    assert parse_elements({"elements": []}, "ramen") == []
    assert parse_elements({}, "ramen") == []

def test_download_retries_then_succeeds_and_waits_between_attempts():
    """Two failures then success: the data is returned and the waits follow the schedule.

    Setup: a fake `post` that raises twice (like Overpass answering 429/504 while busy)
    and then returns one element. The fake `sleep` records how long we WOULD have waited.
    Protects against: no retry (one busy moment kills the whole run) or a changed
    schedule (hammering the server with no pause).
    """ 
    waits, calls = [], []

    def post(url, data, headers):
        calls.append(headers["User-Agent"])
        if len(calls) < 3:
            raise RuntimeError("429 too many requests")
        return {"elements": [{"type": "node", "id": 9, "lat": 34.7, "lon": 135.5, "tags": {}}]}
    rows = download("bus_stop", (34, 135, 35, 136), contact="me@example.com",
                 post=post, sleep=waits.append)

    assert len(rows) == 1 and len(calls) == 3
    assert waits == list(RETRY_DELAYS_SECONDS)              # 30 s then 90 s
    assert "me@example.com" in calls[0]                     # identifying User-Agent sent


def test_download_gives_up_after_last_attempt_and_raises():
   """If every attempt fails the error must surface, not be swallowed.

    Why it matters: replace_amenities only runs after a successful download, so a raised
    error is what protects the existing amenity rows from being wiped by a failed fetch.
    """
   def post(url, data, headers):
       raise RuntimeError("down")

   with pytest.raises(RuntimeError, match="down"):
       download("school", (34, 135, 35, 136), post=post, sleep=lambda s: None) 

