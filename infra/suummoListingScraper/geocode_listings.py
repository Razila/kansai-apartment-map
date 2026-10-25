"""
geocode_listings.py -- turn each listing's text address into map coordinates (lat/lon).

WHY THIS EXISTS
    SUUMO gives us an address as text ("大阪府大阪市北区大淀南２"), never coordinates.
    The map needs a point to draw, and the amenity step (nearest supermarket, bus stop,
    ...) needs a point to measure from. This script fills listings.geom for every
    listing that does not have one yet.

HOW TO RUN (from infra/suummoListingScraper, same folder as load_listings.py):
    python geocode_listings.py                 # geocode every listing that has no geom yet
    python geocode_listings.py --limit 20      # only the first 20 (use this for a first test)
    python geocode_listings.py --retry-failed  # ALSO re-try listings that failed before

WHERE THE COORDINATES COME FROM (in this order)
    1. GSI (国土地理院, Japan's national mapping agency) address search. Free, no key,
       built for Japanese addresses. PRIMARY source.
    2. Nominatim (OpenStreetMap). FALLBACK only, because its usage policy allows about
       1 request per second and requires an identifying User-Agent. It is used only
       when NOMINATIM_CONTACT is set in the repo-root .env (an email or URL that
       identifies you); without it the fallback is switched off and a note is printed.

HOW GOOD ARE THE RESULTS? (read this when a pin looks "off")
    SUUMO addresses stop at the chome level ("大淀南２", no block/building number).
    So a pin is the centre of that chome, typically within a few hundred metres of the
    real building, NOT the building itself. Every row records HOW it was found in
    listings.geocode_source:
        'gsi'          full address (including chome) was found
        'gsi_town'     chome could not be matched, so we used the town ("大淀南")
                       -> less precise; the pin may be ~1 km from the building
        'nominatim'    GSI found nothing, OpenStreetMap did
    Filter on this column when you need to know how much to trust a pin.

SAFETY CHECKS (a wrong pin is worse than no pin)
    * The result's name must contain the listing's own city+ward ("大阪市北区"). Without
      this, "北区" could silently resolve to Kyoto's or Sakai's 北区.
    * The point must fall inside a rough Kansai bounding box.
    A result that fails either check is discarded, as if nothing was found.

FAILURES ARE REMEMBERED
    A listing that cannot be geocoded gets geocode_attempted_at set but geom left NULL.
    Later runs skip it (so we do not hammer the APIs with the same hopeless address
    every day) unless --retry-failed is given.

TROUBLESHOOTING QUICK REFERENCE
    * Everything fails, "GSI error ... 403/Connection": the machine cannot reach
      msearch.gsi.go.jp (firewall/VPN/proxy). Open the URL in GSI_URL in a browser.
    * Pins land in the sea off Africa (lat~0, lon~0) or in the wrong hemisphere:
      latitude/longitude were swapped somewhere. See the "[lon, lat]" notes below.
    * Pins land in the wrong city: the name check (title_matches_listing) was bypassed
      or the listing's city/ward columns are wrong. Look at the row in psql first.
    * "FAIL" for many listings: print build_queries(address) for one of them and paste
      each query into https://msearch.gsi.go.jp/address-search/AddressSearch?q=<query>
      to see what GSI actually answers.

Requires: pip install requests    (psycopg and python-dotenv are already installed)
"""
import argparse   # reads the command-line flags (--limit, --retry-failed)
import os         # reads environment variables (DATABASE_URL, NOMINATIM_CONTACT)
import re         # regular expression used to detect a trailing chome number
import sys        # sys.exit for fatal config errors, sys.stderr for warnings
import time       # time.sleep, used to be polite to the geocoding services
import unicodedata  # Unicode normalisation (full-width -> normal characters)
from pathlib import Path  # builds the path to the repo-root .env file

# --- Settings -------------------------------------------------------------------------
# Kept as named constants at the top so a reviewer can see every tunable in one place
# and so a change (a different geocoder URL, a wider map area) is a one-line edit.

GSI_URL = "https://msearch.gsi.go.jp/address-search/AddressSearch"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

# Rough rectangle around Osaka/Kyoto/Hyogo/Nara/Shiga/Wakayama. A point outside it is
# almost certainly a wrong match (e.g. a same-named town in Tokyo, or swapped lat/lon).
# It is a sanity net, not a legal boundary, so it is deliberately a little generous.
# If you ever add listings outside Kansai, widen these two ranges.
KANSAI_LAT_RANGE = (33.4, 35.8)   # (south, north) in degrees
KANSAI_LON_RANGE = (134.0, 136.8)  # (west, east) in degrees

# Seconds to wait after each geocoder call. GSI publishes no limit but is a free public
# service, so we stay gentle. Nominatim's policy is max 1 request/second; 1.1 s gives a
# small safety margin. Do NOT lower NOMINATIM_DELAY_SECONDS below 1: the usage policy
# is explicit and violators get their IP blocked.
GSI_DELAY_SECONDS = 0.5
NOMINATIM_DELAY_SECONDS = 1.1

# Give up on a single HTTP request after this many seconds, so a hung server cannot
# freeze the whole run. A timeout raises an exception, which geocode_listing() catches.
HTTP_TIMEOUT_SECONDS = 10


# --- Pure helpers (no network, no database: these are what the unit tests cover) ------
# "Pure" means: same input always gives the same output and nothing outside the function
# is touched. That makes them trivially testable and easy to reason about when debugging.

def normalize(text):
    """Return `text` in a form that compares reliably.

    WHAT: applies Unicode NFKC normalisation, then removes ordinary and full-width
          spaces. NFKC converts "compatibility" characters to their plain equivalents:
          full-width digits ("２" -> "2"), full-width Latin ("ＪＲ" -> "JR").
    WHY:  SUUMO writes numbers full-width ("大淀南２") while GSI answers with a mix of
          styles. Comparing or querying without normalising makes identical addresses
          look different and silently breaks matching.
    EDGE: returns "" for None/empty so callers never need a special case for a
          missing ward (many cities, e.g. 吹田市, have no wards).
    """
    if not text:
        return ""
    # NFKC does the full-width -> normal conversion; the two .replace calls strip a
    # normal space and the ideographic (full-width) space "　" that Japanese text
    # sometimes contains between words.
    return unicodedata.normalize("NFKC", text).replace(" ", "").replace("　", "")


def build_queries(address):
    """Return the address spellings to try against the geocoder, best first.

    WHAT: a list of (query_text, label) pairs. `label` is the value that will be saved
          in listings.geocode_source if THAT query is the one that finds a result.
    WHY:  GSI matches text against its own index, so the spelling matters. SUUMO writes
          "大淀南２" (chome as a bare number) but GSI's index contains names like
          "大淀南２丁目". Trying a few spellings, most precise first, finds more
          addresses. Falling back to the town name is a last resort that still gives a
          usable pin, but we record it as 'gsi_town' so its lower precision is visible.
    ORDER:
        1. the address exactly as given                       -> 'gsi'
        2. trailing chome number followed by 丁目              -> 'gsi'
        3. the address with the trailing number removed        -> 'gsi_town'
    """
    # Normalise first so "大淀南２" and "大淀南2" are treated identically.
    base = normalize(address)
    queries = [(base, "gsi")]

    # Detect a chome number: digits at the very END of the string ($) that are NOT part
    # of a block number like "1-2". (?<![\d-]) is a "negative lookbehind": it forbids a
    # digit or hyphen directly before the match, so for "江坂町1-2" the final "2" is
    # rejected (it follows "-") and for "江坂町12" the match cannot start in the middle
    # of the number. Result: only a lone trailing chome number like "大淀南2" matches.
    m = re.search(r"(?<![\d-])(\d+)$", base)
    if m:
        # Variant 2: append 丁目 ("chome") to match how GSI spells it.
        queries.append((base + "丁目", "gsi"))
        # Variant 3: drop the number to search the town alone. m.start() is the index
        # where the digits begin, so base[: m.start()] is everything before them.
        queries.append((base[: m.start()], "gsi_town"))

    # Remove duplicates and empty strings while preserving order. (Variants can
    # coincide, and an address that is only digits would produce an empty town query;
    # sending an empty query would waste a request and could return garbage.)
    seen, result = set(), []
    for text, label in queries:
        if text and text not in seen:
            seen.add(text)
            result.append((text, label))
    return result


def title_matches_listing(title, city, ward):
    """True if a geocoder result's name plausibly refers to this listing's own area.

    `title` is the place name the geocoder returned, e.g. "大阪府大阪市北区大淀南２丁目".
    WHAT: checks that the listing's city + ward (e.g. "大阪市" + "北区") appears inside it.
    WHY:  ward names are not unique in Kansai: 北区 exists in Osaka, Kyoto, Sakai and
          Kobe. GSI's top result for a vague query can easily be the wrong city's 北区.
          Matching on the ward alone would then place the listing in the wrong city
          with no error. Requiring city+ward together prevents that.
    EDGE: cities without wards (e.g. 吹田市) have ward=None, so we match on city alone.
          If both are empty we return False: with nothing to compare, accepting the
          result would defeat the purpose of the check.
    """
    wanted = normalize(city) + normalize(ward)  # e.g. "大阪市北区"; ward may be ""
    # `wanted in ...` is a substring test, so the title may contain the prefecture and
    # chome around it. normalize(title) is needed because GSI titles use full-width
    # digits and we normalise our side as well, keeping both sides comparable.
    return bool(wanted) and wanted in normalize(title)


def in_kansai(lat, lon):
    """True if the point lies inside the rough Kansai rectangle (see KANSAI_*_RANGE).

    Catches two classes of bug that name matching cannot: a result in the right-named
    town in the wrong region (Tokyo has a 北区 too), and swapped latitude/longitude
    (34.7, 135.5 is Osaka; swapped it would be 135.5 degrees latitude, which is not a
    place on Earth).
    """
    return (KANSAI_LAT_RANGE[0] <= lat <= KANSAI_LAT_RANGE[1]
            and KANSAI_LON_RANGE[0] <= lon <= KANSAI_LON_RANGE[1])


# --- Geocoder calls (network; `get` is injectable so tests never touch the internet) --

def default_get(url, params=None, headers=None):
    """Perform an HTTP GET and return the parsed JSON body.

    This is the ONLY function that touches the network. Everything else receives it as
    a parameter named `get`. WHY: tests pass a fake `get` that returns canned data, so
    they run offline, instantly and deterministically, and a broken internet connection
    or a changed API cannot make the unit tests fail.
    """
    # Imported here, not at the top of the file, so the unit tests (which never call
    # this function) work even on a machine where `requests` is not installed yet.
    import requests
    response = requests.get(url, params=params, headers=headers, timeout=HTTP_TIMEOUT_SECONDS)
    # raise_for_status() turns HTTP errors (403 blocked, 429 slow down, 500 server
    # error) into exceptions. Without it we would try to read an error page as if it
    # were data and fail later with a confusing message.
    response.raise_for_status()
    return response.json()


def geocode_gsi(address, city, ward, get=default_get, sleep=time.sleep):
    """Look `address` up on GSI. Return (lat, lon, source_label) or None.

    GSI answers with a JSON list of GeoJSON features, ordered by GSI's own relevance:
        [{"geometry": {"coordinates": [lon, lat]},
          "properties": {"title": "大阪府大阪市北区大淀南２丁目"}}, ...]
    IMPORTANT: GeoJSON order is [LONGITUDE, LATITUDE], the opposite of how people say
    "lat, lon". Reading it the wrong way round puts pins off the coast of Africa, so
    the unpacking below is explicit and covered by a unit test.

    We deliberately do NOT just take the first feature. We take the first feature that
    passes BOTH safety checks (name matches the listing's city/ward; point is in
    Kansai), because GSI's #1 result is sometimes a same-named place elsewhere.
    `sleep` is injectable for the same reason as `get`: tests must not really wait.
    """
    # Try each spelling from most to least precise; stop at the first that yields a
    # result that passes the checks.
    for query, label in build_queries(address):
        features = get(GSI_URL, params={"q": query})
        sleep(GSI_DELAY_SECONDS)  # be polite to a free public service
        # `or []` protects against the API returning null instead of an empty list.
        for feature in features or []:
            # .get with defaults so a feature missing "properties" does not crash the
            # run; an empty title simply fails the name check below.
            title = feature.get("properties", {}).get("title", "")
            # Explicit [lon, lat] unpack (see the docstring warning).
            lon, lat = feature["geometry"]["coordinates"]
            if title_matches_listing(title, city, ward) and in_kansai(lat, lon):
                return lat, lon, label
    return None  # none of the spellings produced an acceptable result


def geocode_nominatim(address, contact, get=default_get, sleep=time.sleep):
    """Fallback: look `address` up on OpenStreetMap's Nominatim. (lat, lon, 'nominatim') or None.

    WHY IT EXISTS: GSI occasionally cannot resolve an address (new developments,
    unusual spellings). OpenStreetMap has different data, so it rescues some of those.
    WHY IT IS ONLY A FALLBACK: its usage policy limits us to ~1 request/second and
    requires an identifying User-Agent, so it is too slow to be the primary source.
    `contact` (an email or URL) is put into the User-Agent header; Nominatim blocks
    the default python-requests one, and an identifying agent lets them contact you
    instead of banning you if your script misbehaves.
    CHECKS: names are in a different style from GSI's, so we cannot do the city/ward
    name comparison here. The Kansai bounding box is the only safety check, which is
    one more reason this source is used second.
    """
    results = get(
        NOMINATIM_URL,
        params={
            "q": normalize(address),
            "format": "json",       # ask for JSON instead of the HTML page
            "countrycodes": "jp",   # never return matches outside Japan
            "limit": 1,             # we only use the top result, so do not download more
        },
        headers={"User-Agent": f"kansai-apartment-map/0.1 ({contact})"},
    )
    sleep(NOMINATIM_DELAY_SECONDS)  # honour the 1-request-per-second policy
    if not results:
        return None
    # Nominatim returns coordinates as STRINGS ("34.70"), so convert them to numbers.
    lat, lon = float(results[0]["lat"]), float(results[0]["lon"])
    return (lat, lon, "nominatim") if in_kansai(lat, lon) else None


def geocode_listing(address, city, ward, nominatim_contact=None, get=default_get, sleep=time.sleep):
    """Geocode one listing: GSI first, Nominatim only if GSI found nothing usable.

    Returns (lat, lon, source_label) or None.

    ERROR POLICY: an exception from one geocoder (timeout, 403, malformed JSON) is
    caught here and treated as "that geocoder had no answer". WHY: a batch job over
    hundreds of listings must not die because ONE request failed. The error text is
    printed to stderr, so the failure is visible and diagnosable rather than silent.
    The `noqa` comments tell the linter that catching a broad Exception is intentional.
    """
    try:
        found = geocode_gsi(address, city, ward, get=get, sleep=sleep)
        if found:
            return found  # primary source succeeded: never bother Nominatim
    except Exception as exc:  # noqa: BLE001 - deliberate, see docstring
        print(f"  GSI error for {address!r}: {exc}", file=sys.stderr)

    # Reached when GSI returned nothing OR raised. Only try the fallback if the user
    # supplied a contact string (required by Nominatim's policy).
    if nominatim_contact:
        try:
            return geocode_nominatim(address, nominatim_contact, get=get, sleep=sleep)
        except Exception as exc:  # noqa: BLE001
            print(f"  Nominatim error for {address!r}: {exc}", file=sys.stderr)
    return None


# --- Database ---------------------------------------------------------------------------

def fetch_pending(conn, limit=None, retry_failed=False):
    """Return [(id, address_ja, city, ward), ...] for listings that still need a pin.

    "Needs a pin" means geom IS NULL and the listing is still active (delisted
    listings are not worth spending requests on). Listings that were already attempted
    and failed (geocode_attempted_at is set) are skipped unless `retry_failed` is True:
    without that rule, one un-geocodable address would be retried on every run forever.
    """
    sql = ("SELECT id, address_ja, city, ward FROM listings "
           "WHERE geom IS NULL AND status = 'active'")
    if not retry_failed:
        sql += " AND geocode_attempted_at IS NULL"
    # Oldest first, so a --limit run is predictable and repeatable.
    sql += " ORDER BY created_at"
    params = ()
    if limit:
        # The limit goes in as a query PARAMETER (%s), never pasted into the SQL text.
        # Habit worth keeping everywhere: parameters cannot be used for SQL injection.
        sql += " LIMIT %s"
        params = (limit,)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def save_result(conn, listing_id, result):
    """Write the outcome for one listing and commit immediately.

    `result` is (lat, lon, source) on success, or None on failure.
      success -> store the point and where it came from
      failure -> store ONLY the attempt time; geom stays NULL. That timestamp is what
                 makes later runs skip the listing (see fetch_pending).

    The SQL, piece by piece:
      ST_MakePoint(lon, lat)  builds a point. It takes (X, Y) = (LONGITUDE, LATITUDE),
                              the reverse of the spoken "lat, lon". This is the single
                              easiest place to introduce a swapped-coordinates bug.
      ST_SetSRID(..., 4326)   labels the point as ordinary GPS lat/lon. Without a
                              label PostGIS cannot tell what the numbers mean.
      ::geography             stores it as a "geography" so distance queries (ST_DWithin,
                              used later for amenities) return real metres, not degrees.
    We commit after EVERY listing so a run interrupted at listing 80 of 100 keeps the 79
    already done instead of rolling them all back.
    """
    with conn.cursor() as cur:
        if result:
            lat, lon, source = result
            cur.execute(
                """UPDATE listings
                   SET geom = ST_SetSRID(ST_MakePoint(%s, %s), 4326)::geography,
                       geocode_source = %s, geocode_attempted_at = now(), updated_at = now()
                   WHERE id = %s""",
                # NOTE the parameter order: lon FIRST, then lat, matching ST_MakePoint.
                (lon, lat, source, listing_id),
            )
        else:
            cur.execute(
                "UPDATE listings SET geocode_attempted_at = now() WHERE id = %s",
                (listing_id,),
            )
    conn.commit()


def run(conn, limit=None, retry_failed=False, nominatim_contact=None,
        get=default_get, sleep=time.sleep):
    """Geocode all pending listings and return a summary dict {'geocoded': n, 'failed': n}.

    WHY THE CACHE: many listings share one building and therefore one address. `cache`
    remembers the answer per (address, city, ward) for the duration of this run, so each
    DISTINCT address costs one lookup instead of one per listing. That means fewer
    requests to a free public service and a faster run. (Failures are cached too: if an
    address failed once this run there is no point asking again.)
    The cache is a plain dict and lives only in memory; it is not saved between runs
    because the database itself is the permanent record (geom / geocode_attempted_at).
    """
    summary = {"geocoded": 0, "failed": 0}
    cache = {}
    for listing_id, address, city, ward in fetch_pending(conn, limit, retry_failed):
        key = (address, city, ward)  # tuples can be dict keys; lists cannot
        if key not in cache:
            cache[key] = geocode_listing(address, city, ward, nominatim_contact, get, sleep)
        result = cache[key]
        save_result(conn, listing_id, result)
        summary["geocoded" if result else "failed"] += 1
        # One progress line per listing: lets you watch a long run and see at a glance
        # which addresses failed.
        print(f"  {'OK  ' if result else 'FAIL'} {address} -> {result}")
    return summary


def main():
    """Command-line entry point: read flags and config, open the database, call run()."""
    # Imported here so merely importing this module (as the unit tests do) does not
    # require a database driver or dotenv to be installed.
    import psycopg
    from dotenv import load_dotenv

    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, help="only process this many listings")
    parser.add_argument("--retry-failed", action="store_true",
                        help="also retry listings that failed on an earlier run")
    args = parser.parse_args()

    # The .env file lives at the repo root: <repo>/infra/<this folder>/geocode_listings.py
    # Path(__file__).resolve() is this file's absolute path; .parents[2] climbs two
    # folders up (suummoListingScraper -> infra -> repo root). Using the file's own
    # location, not the current directory, means it works no matter where you run it from.
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    url = os.environ.get("DATABASE_URL")
    if not url:
        # Stop immediately with a readable message instead of a cryptic connection error.
        sys.exit("DATABASE_URL is not set (expected in the repo-root .env)")
    contact = os.environ.get("NOMINATIM_CONTACT")
    if not contact:
        print("NOMINATIM_CONTACT not set: OpenStreetMap fallback is OFF (GSI only).")

    # `with psycopg.connect(...)` closes the connection automatically at the end,
    # even if an error occurs.
    with psycopg.connect(url) as conn:
        summary = run(conn, args.limit, args.retry_failed, contact)
    print(f"Geocoding finished: {summary}")


if __name__ == "__main__":
    main()
