exports.up = (pgm) => {
  pgm.createExtension('postgis', { ifNotExists: true });


  pgm.createTable('agencies', {
    id: { type: 'uuid', default: pgm.func('gen_random_uuid()'), primaryKey: true },
    name_ja: { type: 'text', notNull: true },
    name_en: { type: 'text' },
    logo_url: { type: 'text' },
  });

  pgm.createTable('listings', {
    id: { type: 'uuid', default: pgm.func('gen_random_uuid()'), primaryKey: true },
    external_id: { type: 'text', notNull: true, unique: true },
    agency_id: { type: 'uuid', references: 'agencies' },
    title_ja: { type: 'text', notNull: true },
    address_ja: { type: 'text', notNull: true },
    prefecture: { type: 'text', notNull: true },
    city: { type: 'text', notNull: true },
    ward: { type: 'text' },
    geom: { type: 'geography(Point,4326)' },
    rent_yen: { type: 'integer', notNull: true },
    requires_shikikin: { type: 'boolean', notNull: true, default: false },
    requires_reikin: { type: 'boolean', notNull: true, default: false },
    shikikin_months: { type: 'numeric' },
    reikin_months: { type: 'numeric' },
    size_sqm: { type: 'numeric' },
    room_layout: { type: 'text' },
    built_year: { type: 'integer' },
    bathroom_type: { type: 'text' },
    has_ac: { type: 'boolean', default: false },
    has_oven: { type: 'boolean', default: false },
    has_washer_hookup: { type: 'boolean', default: false },
    parking_available: { type: 'boolean', default: false },
    pets_allowed: { type: 'boolean', default: false },
    secured_entrance: { type: 'boolean', default: false },
    thumbnail_url: { type: 'text' },
    source_url: { type: 'text' },
    status: { type: 'text', notNull: true, default: 'active' },
    last_seen_at: { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
    created_at: { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
    updated_at: { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
  });

  pgm.createIndex('listings', 'geom', { method: 'gist' });
  pgm.createIndex('listings', 'status');

  pgm.createTable('listing_amenity_distances', {
    listing_id: { type: 'uuid', notNull: true, references: 'listings', onDelete: 'CASCADE' },
    amenity_type: { type: 'text', notNull: true },
    name: { type: 'text', notNull: true },
    distance_m: { type: 'integer' },
    walk_minutes: { type: 'integer' },
  });
  pgm.addConstraint('listing_amenity_distances', 'pk_amenity', {
    primaryKey: ['listing_id', 'amenity_type', 'name'],
  });

  pgm.createTable('raw_listings_archive', {
    external_id: { type: 'text', notNull: true },
    fetched_at: { type: 'timestamptz', notNull: true, default: pgm.func('now()') },
    raw_json: { type: 'jsonb', notNull: true },
  });
};

exports.down = (pgm) => {
  pgm.dropTable('raw_listings_archive');
  pgm.dropTable('listing_amenity_distances');
  pgm.dropTable('listings');
  pgm.dropTable('agencies');
};
