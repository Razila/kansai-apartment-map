/**
 * Adds the columns that detail-page extraction produces but the first schema
 * did not anticipate, and makes agency names unique so the loader can upsert them.
 */
exports.up = (pgm) => {
  pgm.addColumns('listings', {
    // SUUMO's code for the specific agency listing we scraped (the ?bc= value)
    suumo_code: { type: 'text' },
    management_fee_yen: { type: 'integer' },
    // Actual yen amounts. The existing *_months columns hold the same fee as a
    // multiple of rent. NULL amount + requires_* = false means "not charged".
    shikikin_yen: { type: 'integer' },
    reikin_yen: { type: 'integer' },
    // "Anything else" up front (agency fee, guarantor, key exchange...) kept as
    // the original Japanese text per item, e.g. {"保証会社": "...", "ほか初期費用": "..."}
    other_initial_costs: { type: 'jsonb', notNull: true, default: pgm.func("'{}'::jsonb") },
    floor: { type: 'integer' },
    total_floors: { type: 'integer' },
    // Denormalised from listing_amenity_distances so "within N minutes of a
    // station" is a cheap single-column filter.
    nearest_station_walk_min: { type: 'integer' },
    parking_text: { type: 'text' },
    // Raw feature list, so the boolean flags can be recomputed later without re-crawling
    equipment_text: { type: 'text' },
  });

  // Needed for INSERT ... ON CONFLICT (name_ja) when upserting agencies.
  pgm.addConstraint('agencies', 'agencies_name_ja_key', { unique: 'name_ja' });
};

exports.down = (pgm) => {
  pgm.dropConstraint('agencies', 'agencies_name_ja_key');
  pgm.dropColumns('listings', [
    'suumo_code', 'management_fee_yen', 'shikikin_yen', 'reikin_yen',
    'other_initial_costs', 'floor', 'total_floors', 'nearest_station_walk_min',
    'parking_text', 'equipment_text',
  ]);
};
