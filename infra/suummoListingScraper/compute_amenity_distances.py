
"""
compute_amenity_distances.py -- for every listing, find the NEAREST amenity of each
category and store the distance and an estimated walking time.

WHY THIS EXISTS
    Filters like "supermarket within 10 minutes' walk" need a number per listing per
    category. fetch_amenities.py filled the `amenities` table; this script measures from
    each listing to its nearest neighbour in each category and writes the answer to
    listing_amenity_distances (the same table that already holds station walk times).

HOW TO RUN (from infra/suummoListingScraper):
    python compute_amenity_distances.py
    Re-run after: new listings were loaded and geocoded, or amenities were re-fetched.
    It is safe to repeat: it rebuilds these rows from scratch each time (idempotent).

HOW THE NUMBERS ARE PRODUCED -- AN HONEST ESTIMATE
    distance_m    straight-line distance in metres (PostGIS geography, so it accounts for
                  the curvature of the Earth). NOT the walking route.
    walk_minutes  distance x DETOUR_FACTOR / METERS_PER_MINUTE, rounded UP, at least 1.
        METERS_PER_MINUTE = 80   Japan's real-estate advertising rules define 1 minute
                                 of walking as 80 m, which is also how SUUMO's
                                 "徒歩N分" figures are computed. Using the same
                                 convention keeps our numbers comparable.
        DETOUR_FACTOR = 1.3      Streets are not straight lines; city walking routes are
                                 typically 20-40% longer than the crow-flies distance.
                                 1.3 is a middle-of-the-road allowance, NOT measured.
    Every row is labelled method='straight_line_estimate'. Step 5 (OSRM routing) will
    replace these with real routed times and label them 'osrm'.

WHAT "NO ROW" MEANS
    Only amenities within MAX_RADIUS_M (2 km straight-line) are recorded. A listing with
    no 'supermarket' row has no mapped supermarket within 2 km. Later filters treat
    "no row" as "does not satisfy the filter".

Requires the migration add-amenities-table.js to have been applied.
"""

import os
import sys
from pathlib import Path

from fetch_amenities import AMENITY_TYPES

# --- Tunable assumptions (see the module docstring for the reasoning) -----------------
MAX_RADIUS_M = 2000
METERS_PER_MINUTE = 80
DETOUR_FACTOR = 1.3
METHOD_LABEL = "straight_line_estimate"

# The whole job is ONE SQL statement. Read it top to bottom:
#   FROM listings l                     every listing...
#   CROSS JOIN unnest(types) t          ...paired with every category (listing x category)
#   CROSS JOIN LATERAL ( ... ) n        ...and for EACH pair run a small subquery that:
#       WHERE a.amenity_type = t.type   only looks at that category,
#       AND ST_DWithin(..., radius)     only at things within MAX_RADIUS_M (uses the index
#                                       to discard far-away rows cheaply),
#       ORDER BY a.geom <-> l.geom      sorts nearest first. `<->` is PostGIS's
#                                       "distance for ordering" operator; with the GiST
#                                       index it finds the nearest without sorting all rows,
#       LIMIT 1                         and keeps just the nearest one.
#   LATERAL is what lets that subquery refer to the current listing `l` and category `t`;
#   an ordinary join cannot. A listing/category pair with nothing inside the radius gets
#   no row (CROSS JOIN LATERAL drops pairs whose subquery returns nothing).
#   COALESCE(name, '(unnamed)') because the table's primary key needs a non-empty name.
#   CEIL rounds UP, GREATEST(1, ...) ensures a minimum of 1 minute (SUUMO never shows 0).
INSERT_SQL = """
INSERT INTO listing_amenity_distances
    (listing_id, amenity_type, name, distance_m, walk_minutes, method)
SELECT l.id,
        t.amenity_type,
        COALESCE(n.name, '(unnamed)'),
        ROUND(n.dist)::int,
        GREATEST(1, CEIL(n.dist * %(detour)s / %(mpm)s))::int,
        %(method)s
FROM listings l
CROSS JOIN unnest(%(types)s::text[]) AS t(amenity_type)
CROSS JOIN LATERAL (
    SELECT a.name, ST_Distance(a.geom, l.geom) AS dist
    FROM amenities a
    WHERE a.amenity_type = t.amenity_type
        AND ST_DWithin(a.geom, l.geom, %(radius)s)
    ORDER BY a.geom <-> l.geom
    LIMIT 1
)n
WHERE l.geom IS NOT NULL AND l.status = 'active'
"""

def compute(conn, types=None):
     """Rebuild amenity-distance rows for `types` (default: all) and return {type: count}.

    Two statements, in ONE transaction (the `with conn.transaction()` block): either
    both happen or neither does. Doing it in two steps -- delete the old rows, insert
    fresh ones -- means a listing whose nearest supermarket changed (or disappeared)
    ends up correct, which an "insert if missing" approach would not. Station rows are
    untouched because they are not in `types`.
    """
     types = list(types or AMENITY_TYPES)
     with conn.transaction():
          with conn.cursor() as cur:
               cur.execute("DELETE FROM listing_amenity_distances WHERE amenity_type = ANY(%s)", (types,))
               cur.execute(INSERT_SQL, {
                    "types": types, "radius": MAX_RADIUS_M, "detour": DETOUR_FACTOR,
                     "mpm": METERS_PER_MINUTE, "method": METHOD_LABEL
               })
               with conn.cursor() as cur:
                    cur.execute(
                         "SELECT amenity_type, count(*) FROM listing_amenity_distances "
                         "WHERE amenity_type = ANY(%s) GROUP BY 1 ORDER BY 1", (types,))
                    return dict(cur.fetchall())


def main():
     import psycopg
     from dotenv import load_dotenv

     # .env is at the repo root: <repo>/infra/<this folder>/compute_amenity_distances.py
     load_dotenv(Path(__file__).resolve().parents[2] / ".env")
     url = os.environ.get("DATABASE_URL")
     if not url:
          sys.exit("DATABASE_URL is not set (expected in the repo-root .env)")
     with psycopg.connect(url) as conn:
          counts = compute(conn)
     print(f"Listings matched per category: {counts}")


if __name__ == "__main__":
     main()          


                    