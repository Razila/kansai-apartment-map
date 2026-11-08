"""
Integration test for compute_amenity_distances.py's SQL, run against a REAL PostGIS
database. This is the only test that checks the nearest-neighbour query actually works.

WHY A SEPARATE FILE: unlike every other test, this one needs a running database, so it
is SKIPPED unless you opt in by setting TEST_DATABASE_URL. Example (Git Bash, from
infra/suummoListingScraper), reusing your normal dev database:
    export TEST_DATABASE_URL="$(grep '^DATABASE_URL=' ../../.env | cut -d= -f2-)"
    python -m pytest tests/test_amenity_sql.py -v

WHY IT IS SAFE TO RUN AGAINST YOUR REAL DEV DATABASE: it creates TEMPORARY tables with
the same names as the real ones (listings, amenities, listing_amenity_distances).
PostgreSQL looks in the temporary schema first, so the code under test reads and writes
the temporary copies and never touches real data; they vanish when the connection
closes. (This "shadow the real table with a temp table" trick is reusable for testing
any SQL-heavy script.)

Geometry used: points near Osaka station, laid out so the expected answers can be worked
out by hand. 0.001 degrees of latitude is about 111 m.
"""

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_DATABASE_URL"),
    reason="set TEST_DATABASE_URL to run database tests")

from compute_amenity_distances import compute # noqa: E402 - after the skip marker on purpose


@pytest.fixture
def conn():
    """A connection with temporary stand-ins for the three tables, rolled back at the end."""
    import psycopg
    c = psycopg.connect(os.environ["TEST_DATABASE_URL"])
    with c.cursor() as cur:
        # Only the columns the SQL touches. ON COMMIT PRESERVE ROWS keeps the data
        # visible across the transaction boundaries inside compute().
        cur.execute("""
        CREATE TEMP TABLE listings (
            id int PRIMARY KEY, status text, geom geography (Point, 4326)
            ) ON COMMIT PRESERVE ROWS""")
        cur.execute("""
            CREATE TEMP TABLE amenities (
                id serial PRIMARY KEY, amenity_type text, name text, geom geography(Point,4326)
            ) ON COMMIT PRESERVE ROWS""")
        cur.execute("""
            CREATE TEMP TABLE listing_amenity_distances (
                listing_id int, amenity_type text, name text, distance_m int,
                walk_minutes int, method text,
                PRIMARY KEY (listing_id, amenity_type, name)
                ) ON COMMIT PRESERVE ROWS""")
        
    yield c
    c.rollback()
    c.close()


def point(lat, lon):
    """SQL text for a geography point (note ST_MakePoint's lon-first order)."""
    return f"ST_SetSRID(ST_MakePoint({lon}, {lat}), 4326)::geography"


def test_nearest_per_category_distance_minutes_and_exclusions(conn):
    """One scenario that checks every rule of the query at once.

    Listing 1 sits at (34.7000, 135.5000). Amenities placed around it:
      supermarket 'near'    ~111 m north  -> chosen (nearest)
      supermarket 'far'     ~555 m north  -> must NOT be chosen
      convenience 'toofar'  ~2.2 km north -> outside the 2 km radius -> no row at all
      bus_stop (unnamed)    ~111 m east   -> chosen, labelled '(unnamed)'
    Listing 2 is delisted -> must get no rows. Listing 3 has no location -> no rows.
    Also a pre-existing 'station' row for listing 1 must survive the rebuild untouched.
    Expected minutes for 'near': ~111 m x 1.3 / 80 = 1.8 -> rounded UP to 2.
    """
    with conn.cursor() as cur:
        cur.execute(f"INSERT INTO listings VALUES (1, 'active',{point(34.7000,135.5000)}),"
                    f"(2, 'delisted' ,{point(34.7000,135.5000)}),(3,'active',NULL)")
        cur.execute(f"""INSERT INTO amenities (amenity_type, name, geom) VALUES
            ('supermarket', 'near',{point(34.7010,135.5000)}),
            ('supermarket', 'far' ,{point(34.7050,135.5000)}),
            ('convenience_store', 'toofar' ,{point(34.7200,135.5000)}),
            ('bus_stop', NULL,{point(34.7000,135.5011)})""")
        cur.execute("INSERT INTO listing_amenity_distances VALUES (1, 'station','Osaka',NULL,5,NULL)")

    counts = compute(conn)
    assert counts == {"bus_stop": 1, "supermarket": 1}       # convenience_store: none in range


    with conn.cursor() as cur:
        cur.execute("""SELECT listing_id, amenity_type, name, distance_m, walk_minutes, method
                        FROM listing_amenity_distances ORDER BY amenity_type""")
        rows = cur.fetchall()

    by_type = {r[1]: r for r in rows}
    assert set(by_type) == {"bus_stop", "station", "supermarket"}
    assert by_type["supermarket"][2] == "near"
    assert 105 <= by_type ["supermarket"][3] <= 118
    assert by_type["supermarket"][4] == 2
    assert by_type["supermarket"][5] == "straight_line_estimate"
    assert by_type["bus_stop"][2] == "(unnamed)"
    assert all(r[0] == 1 for r in rows)


def test_rerun_is_idempotent_and_picks_up_changes(conn):
    """Running twice gives the same rows; changing the amenities changes the result.

    Protects against: duplicate rows on re-run (the primary key would raise), and stale
    results (a closed shop must stop being 'nearest' once the amenities are re-fetched).
    """
    with conn.cursor() as cur:
        cur.execute(f"INSERT INTO listings VALUES (1, 'active',{point(34.7000,135.5000)})")
        cur.execute(f"""INSERT INTO amenities (amenity_type,name,geom) VALUES
        ('ramen','old shop',{point(34.7010,135.5000)})""")

    assert compute(conn) == {"ramen": 1} # first run: builds the rows
    assert compute(conn) == {"ramen": 1} # second run: same input, must give the same answer

    with conn.cursor() as cur:
        cur.execute("DELETE FROM amenities WHERE name = 'old shop'")
        cur.execute(f"""INSERT INTO amenities (amenity_type,name,geom) VALUES
        ('ramen','new shop',{point(34.7000,135.5100)})""")
    compute(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM listing_amenity_distances WHERE amenity_type='ramen'")
        assert cur.fetchall() == [("new shop",)]