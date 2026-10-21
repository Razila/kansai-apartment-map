"""
load_listings.py -- copy a crawl's output.json into the Postgres database.

Run from the scraper project root (next to scrapy.cfg):
    python load_listings.py                  # load output.json
    python load_listings.py --file other.json
    python load_listings.py --delist-stale   # ALSO mark unseen listings as delisted (see warning)

What it does, per listing:
  1. UPSERT the agency (by name) -> gets an agency id
  2. UPSERT the listing (by external_id = the jnc_... unit id): a re-crawl of a
     listing UPDATES the existing row instead of creating a duplicate
  3. REPLACE that listing's station rows in listing_amenity_distances
  4. Append the scraped record to raw_listings_archive (so a mapping bug never
     forces a re-crawl: reprocess the archive instead)

Everything runs in ONE transaction: either the whole file loads or nothing does.
Rows that can't be loaded (e.g. outside Kansai, so no prefecture/city) are
rejected with a printed reason rather than crashing the run.

Geometry is NOT set here. Scraped listings only have a text address; geom stays
NULL until the geocoding step (Step 6) fills it in.
"""
import argparse
import json
import sys
from pathlib import Path

# Columns copied straight from the scraped item to the listings table.
# These names come from this constant, never from scraped data, which is why
# building SQL text from them below is safe.
LISTING_COLUMNS = [
    "external_id", "suumo_code", "source_url", "title_ja", "address_ja",
    "prefecture", "city", "ward", "rent_yen", "management_fee_yen",
    "requires_shikikin", "requires_reikin", "shikikin_yen", "reikin_yen",
    "shikikin_months", "reikin_months", "other_initial_costs", "room_layout",
    "size_sqm", "built_year", "floor", "total_floors", "nearest_station_walk_min",
    "parking_available", "parking_text", "has_ac", "stove_type",
    "has_washer_hookup", "secured_entrance", "bathroom_type", "pets_negotiable",
    "fixed_term_lease",
    "equipment_text", "thumbnail_url", "agency_id",
]


def dedupe_stations(stations):
    """One row per station name, keeping the shortest walk. (The same station can
    be listed under two lines; the table's key is listing + type + name.)"""
    best = {}
    for s in stations or []:
        name = s["station"]
        if name not in best or s["walk_minutes"] < best[name]["walk_minutes"]:
            best[name] = s
    return list(best.values())


def to_row(item):
    """Map one scraped item to listing-table columns. Raises ValueError if the
    row can't satisfy the table's NOT NULL rules. Pure function, no database."""
    if not item.get("prefecture") or not item.get("city"):
        raise ValueError(f"no prefecture/city parsed from address {item.get('address_ja')!r} (outside Kansai?)")
    row = {
        "external_id": item["listing_id"],   # jnc_... = the unit, shared by all agencies
        **{c: item.get(c) for c in LISTING_COLUMNS if c not in ("external_id", "agency_id")},
    }
    row["other_initial_costs"] = item.get("other_initial_costs") or {}
    return row


def load(conn, items, delist_stale=False):
    from psycopg.types.json import Jsonb

    cols = LISTING_COLUMNS
    upsert_sql = f"""
        INSERT INTO listings ({", ".join(cols)}, status, last_seen_at)
        VALUES ({", ".join(f"%({c})s" for c in cols)}, 'active', now())
        ON CONFLICT (external_id) DO UPDATE SET
            {", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c != "external_id")},
            status = 'active', last_seen_at = now(), updated_at = now()
        RETURNING id, (xmax = 0) AS inserted
    """  # (xmax = 0) is true only for a freshly inserted row, false for an update

    summary = {"inserted": 0, "updated": 0, "rejected": 0, "delisted": 0}
    with conn.transaction(), conn.cursor() as cur:
        for item in items:
            try:
                row = to_row(item)
            except ValueError as err:
                summary["rejected"] += 1
                print(f"REJECTED {item.get('listing_id')}: {err}", file=sys.stderr)
                continue

            # 1. agency
            agency_id = None
            if item.get("agency_name_ja"):
                cur.execute(
                    """INSERT INTO agencies (name_ja, logo_url) VALUES (%s, %s)
                       ON CONFLICT (name_ja) DO UPDATE
                       SET logo_url = COALESCE(EXCLUDED.logo_url, agencies.logo_url)
                       RETURNING id""",
                    (item["agency_name_ja"], item.get("agency_logo_url")),
                )
                agency_id = cur.fetchone()[0]

            # 2. listing
            row["agency_id"] = agency_id
            row["other_initial_costs"] = Jsonb(row["other_initial_costs"])
            cur.execute(upsert_sql, row)
            listing_pk, inserted = cur.fetchone()
            summary["inserted" if inserted else "updated"] += 1

            # 3. stations: replace, so a station SUUMO dropped doesn't linger
            cur.execute(
                "DELETE FROM listing_amenity_distances WHERE listing_id = %s AND amenity_type = 'station'",
                (listing_pk,),
            )
            for s in dedupe_stations(item.get("stations")):
                cur.execute(
                    """INSERT INTO listing_amenity_distances (listing_id, amenity_type, name, walk_minutes)
                       VALUES (%s, 'station', %s, %s)""",
                    (listing_pk, s["station"], s["walk_minutes"]),
                )

            # 4. archive
            cur.execute(
                "INSERT INTO raw_listings_archive (external_id, raw_json) VALUES (%s, %s)",
                (row["external_id"], Jsonb(item)),
            )

        if delist_stale:
            # now() is the transaction's start time, so every listing touched above
            # has last_seen_at == now() and is NOT matched by the strict "<".
            cur.execute(
                "UPDATE listings SET status = 'delisted', updated_at = now() "
                "WHERE status = 'active' AND last_seen_at < now()"
            )
            summary["delisted"] = cur.rowcount
    return summary


def main():
    import os
    import psycopg
    from dotenv import load_dotenv

    parser = argparse.ArgumentParser()
    parser.add_argument("--file", default="output.json")
    parser.add_argument(
        "--delist-stale", action="store_true",
        help="mark active listings NOT in this file as delisted. Only safe after a "
             "COMPLETE crawl: with a max_pages cap or an interrupted run, 'not seen' "
             "does not mean 'rented out'.",
    )
    args = parser.parse_args()

    # .env lives at the repo root: <repo>/infra/<this folder>/load_listings.py
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("DATABASE_URL is not set (expected in the repo-root .env)")

    items = json.loads(Path(args.file).read_text(encoding="utf-8"))
    with psycopg.connect(url) as conn:
        summary = load(conn, items, delist_stale=args.delist_stale)
    print(f"Loaded {args.file}: {summary}")


if __name__ == "__main__":
    main()
