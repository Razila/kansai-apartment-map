"""
fetch_amenities.py -- download nearby amenities from OpenStreetMap into our database.

WHY THIS EXISTS
    Users want to filter listings by distance to supermarkets, convenience stores, bus
    stops, ramen shops and schools. SUUMO does not give us that, so we get it from
    OpenStreetMap (OSM): free, open data with no scraping/terms risk. This script is the
    DOWNLOAD half; compute_amenity_distances.py is the MEASURING half.

HOW IT WORKS (big picture)
    1. Work out a map rectangle (bounding box) that covers every geocoded listing, plus
       a margin so amenities just outside the outermost listings are included.
    2. For each category, send ONE query to the Overpass API (OSM's read-only query
       service) asking for everything of that category inside the rectangle.
    3. Replace that category's rows in the `amenities` table with the fresh download.

HOW TO RUN (from infra/suummoListingScraper):
    python fetch_amenities.py                       # all five categories
    python fetch_amenities.py --types supermarket   # just one (good for a first test)
    python fetch_amenities.py --margin 0.05         # wider margin around the listings

    Optional .env settings:
      OVERPASS_URL      use another Overpass server (default: the main public one)
      OVERPASS_CONTACT  your email/URL, added to the User-Agent. Overpass asks heavy
                        users to identify themselves; it is polite, not required.

HOW OFTEN: amenities change slowly. Re-run roughly monthly (or when your listings
expand to a new area), then re-run compute_amenity_distances.py.

KNOWN LIMITS (read when a result looks wrong)
    * OSM is volunteer-edited. Coverage is good for convenience stores and
      supermarkets in cities, patchier for bus stops and for small restaurants, so
      "no ramen shop nearby" can mean "none mapped", not "none exist".
    * Schools come from OSM here, not from the Japanese government dataset (国土数値情報)
      the original plan mentioned. That avoids installing GDAL (hard on Windows). The
      government data is more complete and can be swapped in later without changing the
      rest of the pipeline: it only has to fill the same `amenities` table.
    * Overpass is a shared free service: queries can time out when it is busy. This
      script retries automatically (see download()), but if it keeps failing set
      OVERPASS_URL to a mirror or try again later.

Requires: pip install requests
"""
import argparse
import os
import sys
import time
from pathlib import Path

AMENITY_FILTERS = {
    "supermarket":        'nwr["shop"="supermarket"]',
    "convenience_store":  'nwr["shop"="convenience"]',
    "ramen":              'nwr["amenity"="restaurant"]["cuisine"~"(^|;)ramen(;|$)"]',
    "bus_stop":           'nwr["highway"="bus_stop"]',
    "school":             'nwr["amenity"="school"]',
}
AMENITY_TYPES = list(AMENITY_FILTERS) # the five category names, in a stable order


# Only these OSM tags are copied into amenities.tags. Everything else is dropped to keep
# the table small. Add a key here (and re-fetch) if a future filter needs it.
#   isced:level = school level (1 elementary, 2 junior high, 3 high school)
KEEP_TAGS = ["name", "name:ja", "name:en", "brand", "operator", "isced:level"]

DEFAULT_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_TIMEOUT_SECONDS = 180      # how long we tell the SERVER it may work on a query
HTTP_TIMEOUT_SECONDS = 240          # how long WE wait for its answer (server time + transfer)
PAUSE_BETWEEN_QUERIES_SECONDS = 10  # politeness gap between categories
RETRY_DELAYS_SECONDS = (30, 90)     # waits before retry 2 and retry 3 (3 attempts total)

# --- Pure helpers (no network / database; these are what the unit tests cover) ---------

def build_query(amenity_type, bbox): 
     """Return the Overpass query text for one category inside one rectangle.

    `bbox` is (south, west, north, east) in degrees. Overpass wants exactly that order,
    which is NOT the (lon, lat) order used elsewhere in this project; mixing them up
    returns an empty result or the wrong part of the world.
    Query anatomy:
        [out:json]           answer as JSON
        [timeout:180]        server may spend up to 180 s before giving up
        ( <filter>(bbox); ); the filter restricted to the rectangle
        out center tags;     for each hit print its tags, and for buildings/areas
                             ("ways") also print `center` = a representative point,
                             because we store every amenity as a single point.
    """
     south, west, north, east = bbox
     flt = AMENITY_FILTERS[amenity_type]
     return (
        f"[out:json][timeout:{OVERPASS_TIMEOUT_SECONDS}];\n"
        f"({flt}({south},{west},{north},{east}););\n"
        f"out center tags;"
    )


def bbox_with_margin(min_lat, min_lon, max_lat, max_lon, margin_deg):
     """Grow a rectangle by `margin_deg` degrees on every side; return (S, W, N, E).

    WHY: we measure out to 2 km (see compute_amenity_distances.py). A listing on the
    edge of our area needs amenities up to 2 km beyond it. 0.03 degrees is about 3.3 km
    north-south and 2.7 km east-west in Kansai, comfortably more than 2 km. Values are
    rounded to 5 decimals (~1 m) just to keep the query text tidy.
    """
     return(round(min_lat - margin_deg, 5), round(min_lon - margin_deg, 5),
            round(max_lat + margin_deg, 5), round(max_lon + margin_deg, 5))


def parse_elements(payload, amenity_type):
     """Turn an Overpass JSON answer into a list of plain dicts ready to insert.

    Overpass returns {"elements": [ {...}, ... ]}. Each element is one of:
      node:  {"type":"node","id":1,"lat":34.7,"lon":135.5,"tags":{...}}
      way:   {"type":"way","id":2,"center":{"lat":..,"lon":..},"tags":{...}}
    so a node has its own lat/lon while a way has them under "center". Elements with no
    usable coordinates are skipped rather than crashing the run (it happens with
    incomplete data at the edge of a query).
    Name preference: Japanese name (name:ja), then plain name, then English. Unnamed
    things (many bus stops) keep name=None; the distance step labels them "(unnamed)".
    """
     rows = []
     for el in payload.get("elements", []):
        tags = el.get("tags", {})
        if "lat" in el and "lon" in el:          # node: coordinates are on the element
            lat, lon = el["lat"], el["lon"]
        elif "center" in el:                      # way/relation: use the computed centre
            lat, lon = el["center"]["lat"], el["center"]["lon"]
        else:
            continue                              # no location -> cannot be mapped
        rows.append({
            "osm_type": el["type"],
            "osm_id": el["id"],
            "amenity_type": amenity_type,
            "name": tags.get("name:ja") or tags.get("name") or tags.get("name:en"),
            "lat": lat,
            "lon": lon,
            # Dict comprehension: keep only whitelisted keys that are actually present.
            "tags": {k: tags[k] for k in KEEP_TAGS if k in tags},
        })
     return rows
# --- Network ----------------------------------------------------------------------------
def default_post(url, data, headers):
    """POST `data` to `url` and return the parsed JSON answer. The ONLY network function.
    Injectable (tests pass a fake) for the same reason as in geocode_listings.py: tests
    stay offline and deterministic. We POST rather than GET because Overpass queries can
    be long and URLs have length limits. raise_for_status() turns HTTP errors (429 "too
    many requests", 504 "gateway timeout" -- both common when Overpass is busy) into
    exceptions, so download() can retry instead of parsing an error page.
    """

    import requests # lazy import: unit tests never call this, so they do not need requests
    response = requests.post(url, data=data, headers=headers, timeout= HTTP_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def download(amenity_type, bbox, url=DEFAULT_OVERPASS_URL, contact=None,
             post=default_post, sleep=time.sleep):
    """Download one category and return parse_elements()'s rows. Retries on failure.

    Overpass is shared and free, so transient failures are normal. We try up to
    3 times, waiting 30 s then 90 s between attempts (RETRY_DELAYS_SECONDS), which is
    long enough for a busy server to recover without hammering it. ANY exception counts
    as a failed attempt (timeouts, 429, 504, bad JSON). If all attempts fail, the last
    exception is re-raised so the run stops visibly instead of silently skipping a
    category (and, importantly, the old rows for that category are NOT deleted: see
    replace_amenities, which only runs after a successful download).
    """

    agent = "kansai-apartment-map/0.1" + (f" ({contact})" if contact else "")
    query = build_query(amenity_type, bbox)
    attempts = len(RETRY_DELAYS_SECONDS) + 1
    for attempt in range(1, attempts + 1):
        try:
            payload = post(url, data={"data": query}, headers={"User-Agent": agent})
            return parse_elements(payload, amenity_type)
        except Exception as exc: # noqa: BLE001 - deliberate: any failure -> retry
            if attempt == attempts:
                raise
            wait = RETRY_DELAYS_SECONDS[attempt - 1]
            print(f"  {amenity_type}: attempt {attempt} failed ({exc}); retrying in {wait}s",
                  file=sys.stderr)
            sleep(wait)


def listings_bbox(conn):
    """Return (min_lat, min_lon, max_lat, max_lon) over active, geocoded listings, or None.

    Computed from the data itself so the download automatically follows wherever your
    listings are: adding Kobe listings later widens the rectangle on the next run.
    geom::geometry converts geography to geometry only so ST_X/ST_Y (longitude /
    latitude accessors) can be used. Returns None when there are no geocoded listings.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT min(ST_Y(geom::geometry)), min(ST_X(geom::geometry)),
                   max(ST_Y(geom::geometry)), max(ST_X(geom::geometry))
            FROM listings WHERE geom IS NOT NULL AND status = 'active'""")
        row = cur.fetchone()
    return None if row[0] is None else row


def replace_amenities(conn, amenity_type, rows):
    """Replace every stored row of `amenity_type` with `rows`, in one transaction.

    WHY REPLACE INSTEAD OF MERGE: a shop that closed (or was deleted from OSM) must
    disappear from our data; merging would keep it forever. Doing delete+insert inside a
    single transaction means readers never see a half-empty table, and if the insert
    fails the delete is rolled back, leaving the old data intact.
    ON CONFLICT DO NOTHING guards against the same OSM object appearing twice in one
    download (violating the unique constraint would otherwise abort the whole batch).
    ST_MakePoint takes (LONGITUDE, LATITUDE), the reverse of the spoken order.
    """

    from psycopg.types.json import Jsonb  # wraps a Python dict so Postgres stores it as JSON
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("DELETE FROM amenities WHERE amenity_type = %s", (amenity_type,))
            cur.executemany(
                """INSERT INTO amenities (osm_type, osm_id, amenity_type, name, geom, tags)
                    VALUES (%(osm_type)s, %(osm_id)s, %(amenity_type)s, %(name)s,
                            ST_SetSRID(ST_MakePoint(%(lon)s, %(lat)s), 4326)::geography,
                            %(tags)s)
                    ON CONFLICT ON CONSTRAINT amenities_osm_unique DO NOTHING""",
                [{**r, "tags": Jsonb(r["tags"])} for r in rows], 
            )

def main():
    import psycopg
    from dotenv import load_dotenv

    parser = argparse.ArgumentParser()
    parser.add_argument("--types", default=",".join(AMENITY_TYPES),
                        help=f"comma-separated subset of: {', '.join(AMENITY_TYPES)}")
    parser.add_argument("--margin", type=float, default=0.03,
                        help="degrees to grow the listings' rectangle by (default 0.03 = ~3 km)")
    
    args = parser.parse_args()

    wanted = [t.strip() for t in args.types.split(",") if t.strip()]
    unknown = [t for t in wanted if t not in AMENITY_FILTERS]
    if unknown: # fail early with a clear message instead of a KeyError mid-run
        sys.exit(f"Unknown type(s): {unknown}, Choose from: {AMENITY_TYPES}")

    # .env is at the repo root: <repo>/infra/<this folder>/fetch_amenities.py
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        sys.exist("DATABASE_URL is not set (expected in the repo root .env)")
    overpass_url = os.environ.get("OVERPASS_URL", DEFAULT_OVERPASS_URL)
    contact = os.environ.get("OVERPASS_CONTACT")

    with psycopg.connect(db_url) as conn:
        box = listings_bbox(conn)
        if box is None:
            sys.exit("No geocoded active listings yet: run geocode_listings.py first")
        bbox = bbox_with_margin(*box, margin_deg=args.margin)
        print(f"Area (south, west, north, east): {bbox}")

        for i, amenity_type in enumerate(wanted):
            if i:
                time.sleep(PAUSE_BETWEEN_QUERIES_SECONDS)
            rows = download(amenity_type, bbox, overpass_url, contact)
            replace_amenities(conn, amenity_type, rows)
            print(f" {amenity_type}: stored {len(rows)}")
    print("Done. Next: python compute_amenity_distances.py")


if __name__ == "__main__":
    main()





