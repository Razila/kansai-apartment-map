/**
 * Follow-up to 1792379785521_add-listing-detail-columns, which is already applied
 * and must not be edited. This changes only what that migration did not cover:
 * two new columns, and two columns whose meaning turned out to be wrong.
 */
exports.up = (pgm) => {
  pgm.addColumns('listings', {
    // 'IH' | 'stove' | 'gas_ready' | NULL  (see classify_stove in extract.py)
    stove_type: { type: 'text' },
    // true = 定期借家 (fixed-term lease), false = 普通借家, NULL = unknown
    fixed_term_lease: { type: 'boolean' },
  });

  // ペット相談 means "pets: consult the landlord", i.e. negotiable, not "allowed".
  pgm.renameColumn('listings', 'pets_allowed', 'pets_negotiable');

  // Japanese rentals list stoves, not ovens; stove_type replaces this flag.
  pgm.dropColumns('listings', ['has_oven']);
};

exports.down = (pgm) => {
  pgm.addColumns('listings', { has_oven: { type: 'boolean', default: false } });
  pgm.renameColumn('listings', 'pets_negotiable', 'pets_allowed');
  pgm.dropColumns('listings', ['stove_type', 'fixed_term_lease']);
};
