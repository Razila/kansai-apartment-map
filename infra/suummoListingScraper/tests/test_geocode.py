"""
Offline tests for geocode_listings.py -- no internet and no database needed.

HOW THESE TESTS WORK
    The geocoder functions take an injectable `get` (the function that does the HTTP
    request) and an injectable `sleep`. Here we pass FAKES: a `get` that returns canned
    answers and a `sleep` that does nothing. So the tests check OUR logic (building
    query spellings, the safety checks, ordering, fallback rules) while being fast,
    deterministic, and independent of GSI/Nominatim being reachable.

WHAT THESE TESTS DO NOT COVER (and where that is checked instead)
    * That GSI/Nominatim really answer in the format we assume -> the manual test in
      the step instructions (geocode real listings, look at the pins on a map).
    * The SQL in fetch_pending/save_result -> checked by running the script against the
      real database and the psql query in the step instructions.

WHEN ONE FAILS
    Each test's docstring says what behaviour it protects and what real-world mistake
    would make it fail, so you can tell a genuine regression from an outdated test.

Run from infra/suummoListingScraper:   python -m pytest tests/test_geocode.py -v
"""
import pytest

from geocode_listings import (
    build_queries, geocode_gsi, geocode_listing, in_kansai, normalize,
    title_matches_listing,
)

# Passed as `sleep=` so the code under test does not really pause between requests
# (the real delays are 0.5-1.1 seconds each, which would make the suite slow).
NO_SLEEP = lambda seconds: None  # noqa: E731 - a one-line stand-in, clearer as a lambda


def feature(title, lat, lon):
    """Build one GSI-shaped search result, so each test reads as data, not boilerplate.

    NOTE the argument order is (lat, lon) because that is how humans think, but the
    dictionary stores [lon, lat] because that is what the REAL GSI API returns
    (GeoJSON order). That deliberate mismatch is exactly what the code under test must
    handle correctly.
    """
    return {"geometry": {"coordinates": [lon, lat]}, "properties": {"title": title}}


def fake_get(responses):
    """Return a stand-in for the HTTP `get` that answers from a dict.

    `responses` maps a query string ("大阪府大阪市北区大淀南2") to the list of results
    GSI would return for it. Any query not in the dict returns [] (= "GSI found
    nothing"). The returned function also records every query it was asked, in order,
    in `get.calls`, so a test can assert WHICH queries were tried and in what order.
    """
    def get(url, params=None, headers=None):
        # Same signature as the real default_get so it can be swapped in directly.
        get.calls.append((url, params.get("q") if params else None))
        return responses.get(params.get("q"), []) if params else []
    get.calls = []  # attach the call log to the function object itself
    return get


# ---------------------------------------------------------------------------------------
# normalize / build_queries: getting the SPELLING of the address right
# ---------------------------------------------------------------------------------------

def test_normalize_converts_fullwidth_digits_and_strips_spaces():
    """SUUMO writes digits full-width ("２"); everything downstream compares half-width.

    Protects against: a missing/changed normalisation step, which would make the same
    address look different to the geocoder and to the name check, so lookups quietly
    fail. Also checks None is tolerated (listings without a ward pass None).
    """
    assert normalize("大淀南２ ") == "大淀南2"   # full-width 2 -> 2, trailing space removed
    assert normalize(None) == ""                # missing value -> empty string, no crash


def test_build_queries_adds_chome_and_town_forms_for_trailing_number():
    """An address ending in a chome number must yield three spellings, best first.

    Protects against: someone reordering the list (the precise spelling MUST come
    before the coarse town-only one, or every pin would silently become low-precision)
    or dropping the 丁目 variant (GSI indexes "大淀南２丁目", not "大淀南２").
    The third item's label 'gsi_town' is what lets us find low-precision pins later.
    """
    assert build_queries("大阪府大阪市北区大淀南２") == [
        ("大阪府大阪市北区大淀南2", "gsi"),         # as given (normalised)
        ("大阪府大阪市北区大淀南2丁目", "gsi"),      # spelled the way GSI's index spells it
        ("大阪府大阪市北区大淀南", "gsi_town"),      # last resort: town only
    ]


def test_build_queries_leaves_block_numbers_alone():
    """"1-2" is a block number, not a chome, so no 丁目 / town-only variants are made.

    Protects against: the regex wrongly treating the "2" in "江坂町1-2" as a chome
    number, which would produce nonsense queries like "江坂町1-2丁目" and a town-only
    fallback that throws away the block number we do have.
    """
    assert build_queries("大阪府吹田市江坂町1-2") == [("大阪府吹田市江坂町1-2", "gsi")]


# ---------------------------------------------------------------------------------------
# title_matches_listing / in_kansai: the SAFETY CHECKS that reject wrong pins
# ---------------------------------------------------------------------------------------

def test_title_matches_requires_city_and_ward():
    """A result must name the listing's own city AND ward to be accepted.

    Protects against the most damaging failure mode: 北区 exists in Osaka, Kyoto, Sakai
    and Kobe, so matching on the ward alone would drop an Osaka listing into Kyoto
    with no error. Three cases: right area, wrong city, and a ward-less city.
    """
    assert title_matches_listing("大阪府大阪市北区大淀南２丁目", "大阪市", "北区")
    # Same ward name, different city: must be REJECTED.
    assert not title_matches_listing("京都府京都市北区紫野", "大阪市", "北区")
    # City without wards (ward=None) matches on the city alone.
    assert title_matches_listing("大阪府吹田市江坂町", "吹田市", None)


@pytest.mark.parametrize("lat,lon,expected", [
    (34.70, 135.50, True),    # Osaka: inside the box
    (35.68, 139.76, False),   # Tokyo: right country, wrong region
    (0.0, 0.0, False),        # "null island": what you get from missing/zeroed coordinates
])
def test_in_kansai(lat, lon, expected):
    """The bounding box accepts Kansai points and rejects points elsewhere.

    parametrize runs this one function once per row above, each reported separately.
    Protects against: the box being widened/shifted by accident, and against a missing
    coordinate (0, 0) being accepted as a real location.
    """
    assert in_kansai(lat, lon) is expected


# ---------------------------------------------------------------------------------------
# geocode_gsi: choosing the right result from what GSI returns
# ---------------------------------------------------------------------------------------

def test_gsi_skips_wrong_city_result_and_takes_the_matching_one():
    """GSI's FIRST result is not always right; we must take the first CORRECT one.

    Setup: GSI returns a Kyoto 北区 result first and the real Osaka one second.
    Protects against: a "simplification" to `features[0]`, which would pin an Osaka
    listing in Kyoto. The expected value is the Osaka point (34.70, 135.49).
    """
    get = fake_get({"大阪府大阪市北区大淀南2": [
        feature("京都府京都市北区大淀", 35.05, 135.75),   # first in the list, but WRONG city
        feature("大阪府大阪市北区大淀南２丁目", 34.70, 135.49),  # the correct one
    ]})
    assert geocode_gsi("大阪府大阪市北区大淀南２", "大阪市", "北区", get, NO_SLEEP) == (34.70, 135.49, "gsi")


def test_gsi_returns_lat_before_lon():
    """Guards the [lon, lat] -> (lat, lon) swap, the classic geocoding bug.

    GSI sends GeoJSON order [lon, lat]. If someone "fixes" the unpacking the wrong way,
    every pin lands in the wrong place. Asserting on RANGES (lat 34-35, lon 135-136)
    makes the failure obvious: swapped values would be 135.x for latitude.
    """
    get = fake_get({"大阪府大阪市北区大淀南2": [feature("大阪府大阪市北区大淀南", 34.70, 135.49)]})
    lat, lon, _ = geocode_gsi("大阪府大阪市北区大淀南２", "大阪市", "北区", get, NO_SLEEP)
    assert 34 < lat < 35 and 135 < lon < 136


def test_gsi_falls_back_to_chome_then_town_and_labels_town():
    """If the precise spellings find nothing, fall back to the town and say so.

    Setup: GSI only knows the town-only spelling. Two things are verified:
      1. the label is 'gsi_town' (so low-precision pins can be identified later);
      2. the queries were tried in the documented order, most precise first
         (checked through the call log recorded by fake_get).
    """
    get = fake_get({"大阪府大阪市北区大淀南": [feature("大阪府大阪市北区大淀南", 34.70, 135.49)]})
    result = geocode_gsi("大阪府大阪市北区大淀南２", "大阪市", "北区", get, NO_SLEEP)
    assert result[2] == "gsi_town"
    assert [q for _, q in get.calls] == [
        "大阪府大阪市北区大淀南2", "大阪府大阪市北区大淀南2丁目", "大阪府大阪市北区大淀南"]


def test_gsi_rejects_result_outside_kansai():
    """A result whose NAME matches but whose POINT is outside Kansai is discarded.

    Setup: the title says Osaka-city-Kita-ward but the coordinates are in Tokyo
    (corrupt data or a bad match). The name check passes, so only the bounding-box check
    can catch it. Expected: None (treated as "not found") rather than a Tokyo pin.
    """
    get = fake_get({"大阪府大阪市北区大淀南2": [feature("大阪府大阪市北区大淀南", 35.68, 139.76)]})
    assert geocode_gsi("大阪府大阪市北区大淀南２", "大阪市", "北区", get, NO_SLEEP) is None


# ---------------------------------------------------------------------------------------
# geocode_listing: fallback and error-handling policy
# ---------------------------------------------------------------------------------------

def test_listing_falls_back_to_nominatim_only_when_contact_given():
    """Nominatim is the fallback, used only when GSI finds nothing AND a contact is set.

    Setup: a fake `get` that behaves like both services: GSI (any URL not containing
    "nominatim") returns nothing; Nominatim returns a valid Osaka point (as STRINGS,
    like the real API).
    Two assertions:
      1. with a contact, we get the Nominatim result, and the assert INSIDE the fake
         proves the identifying User-Agent header was really sent (Nominatim's policy
         requires it; omitting it gets the IP blocked).
      2. without a contact, the fallback is OFF and the answer is None.
    """
    def get(url, params=None, headers=None):
        if "nominatim" in url:
            assert "mail@example.com" in headers["User-Agent"]   # identifying UA is sent
            return [{"lat": "34.70", "lon": "135.49"}]
        return []                                                # GSI finds nothing
    args = ("大阪府大阪市北区大淀南２", "大阪市", "北区")
    assert geocode_listing(*args, nominatim_contact="mail@example.com", get=get, sleep=NO_SLEEP) == (34.70, 135.49, "nominatim")
    assert geocode_listing(*args, nominatim_contact=None, get=get, sleep=NO_SLEEP) is None


def test_network_error_is_swallowed_not_raised():
    """One failing request must not crash a batch of hundreds of listings.

    Setup: every HTTP call raises (simulating a timeout/outage). Expected: the function
    returns None (this listing failed) instead of propagating the exception. The error
    is printed to stderr by the code under test, so it is not silent in real use.
    Protects against: removing the try/except, which would abort the whole run on the
    first bad response and leave later listings unprocessed.
    """
    def get(url, params=None, headers=None):
        raise RuntimeError("boom")
    assert geocode_listing("大阪府大阪市北区大淀南２", "大阪市", "北区", get=get, sleep=NO_SLEEP) is None
