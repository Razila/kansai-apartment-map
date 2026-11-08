/**
(amenity enrichment) database changes
 *
 * WHY A SEPARATE `amenities` TABLE
 *   The original plan asked the Overpass API (OpenStreetMap's query service) about
 *   each listing one at a time. That is slow, hammers a free public service, and
 *   re-asks the same question for every listing in the same neighbourhood. Instead we
 *   download ALL supermarkets / convenience stores / bus stops / ramen shops / schools
 *   for the area ONCE into this table, then let PostGIS work out "nearest X to each
 *   listing" locally in a single SQL statement. Local = fast, free, repeatable, and it
 *   still works if Overpass is down.
 *
 * COLUMNS
 *   osm_type, osm_id  OpenStreetMap's own identity for the thing ('node'/'way'/
 *                     'relation' + a number). Lets us trace a row back to openstreetmap.org
 *                     when a pin looks wrong.
 *   amenity_type      OUR category: 'supermarket' | 'convenience_store' | 'ramen' |
 *                     'bus_stop' | 'school'. Not OSM's tag names.
 *   name              Best available name (Japanese preferred) or NULL if unnamed
 *                     (common for bus stops).
 *   geom              Location as a PostGIS geography point (lat/lon, distances in metres),
 *                     the same type as listings.geom so the two can be compared directly.
 *   tags              A small whitelisted subset of OSM tags as JSON (brand, school level,
 *                     ...), kept so later filters need no re-download.
 *   fetched_at        When this row was downloaded; tells you how stale the data is.
 *
 * ALSO: listing_amenity_distances.method
 *   Records HOW a distance/minutes value was produced. Right now every amenity row is
 *   'straight_line_estimate'. Step 5 will overwrite them with real routed walking times
 *   and mark them 'osrm', so the database always tells you how much to trust a number.
 *   Station rows (which come straight from SUUMO's page) keep method NULL.
 */

 exports.up = (pgm) => {
    pgm.createTable('amenities', {
        id: { type: 'bigserial', primaryKey: true },
        osm_type: { type: 'text', notNull: true },
        osm_id: { type: 'bigint', notNull: true },
        amenity_type: { type: 'text', notNull: true },
        name: { type: 'text' },
        geom: { type: 'geography(Point, 4326)', notNull: true },
        tags: {type: 'jsonb', notNull: true, default: pgm.func("'{}'::jsonb") },
        fetched_at: { type: 'timestamptz', notNull: true, default: pgm.func('now()') }   
    });

  // One row per OSM object per category. The same object could legitimately appear in
  // two categories, hence the third column. This also lets the loader use
  // ON CONFLICT DO NOTHING safely.
  pgm.addConstraint('amenities', 'amenities_osm_unique', { 
    unique: ['osm_type', 'osm_id', 'amenity_type'], 
  });

  // GiST = the spatial index. Without it, "nearest amenity to this point" would compare
  // against every row; with it, PostGIS jumps straight to the neighbours.
  pgm.createIndex('amenities', 'geom', { method: 'gist' });
  // Every nearest-neighbour lookup filters by category first.
  pgm.createIndex('amenities', 'amenity_type');

  pgm.addColumns('listing_amenity_distances', {
    method: { type: 'text' },
  });
 };

 // down() exactly reverses up() so the migration can be rolled back if ever needed.
 exports.down = (pgm) => {
  pgm.dropColumns('listing_amenity_distances', ['method']);
  pgm.dropTable('amenities');
};