/**
 * Records HOW and WHEN each listing was geocoded (see geocode_listings.py).
 *
 * WHY A MIGRATION: the database schema is changed only through migration files, never
 * by hand, so every environment (your laptop, the future server) ends up with the
 * same structure and the change history is in version control.
 *
 * geocode_source:       'gsi' | 'gsi_town' | 'nominatim' | NULL (never geocoded).
 *                       'gsi_town' means only the town (not the chome) was matched, so
 *                       the pin is less precise; this column lets us find and treat
 *                       those pins differently later (e.g. show a larger uncertainty
 *                       radius, or re-try them).
 * geocode_attempted_at: set on EVERY attempt, success or failure. A row with this set
 *                       and geom NULL has FAILED; later runs skip it instead of
 *                       retrying the same hopeless address forever (use
 *                       --retry-failed to override).
 *
 * Both columns are nullable with no default on purpose: existing rows are simply
 * "never attempted", which is the true state.
 */
exports.up = (pgm) => {
  pgm.addColumns('listings', {
    geocode_source: { type: 'text' },
    geocode_attempted_at: { type: 'timestamptz' },
  });
};

// down() must exactly undo up(), so the migration can be rolled back cleanly
// (`npx node-pg-migrate down`) if it ever has to be.
exports.down = (pgm) => {
  pgm.dropColumns('listings', ['geocode_source', 'geocode_attempted_at']);
};
