"""Drop the QSO payload column.

Measured before removal, against production: the column held 760 bytes per row,
2,668 MB in total, 71.7% of the `qsos` heap — and every sampled row's value was
byte-identical to the `payload` of the raw event that produced it. No query
selected it, and the rebuild reconstructs payloads from `raw_events`, never from
here, so its only consumer was the code that wrote it.

Two operational notes, both deliberate:

* ``RemoveField`` compiles to ``ALTER TABLE ... DROP COLUMN``, which on
  PostgreSQL 16 is metadata-only and returns immediately whatever the table
  size. The space is *not* returned by this migration: live tuples keep the
  column's bytes until they are rewritten, and a plain ``VACUUM`` does not
  rewrite a table. ``VACUUM FULL qsos`` (or the next compaction pass, which
  already does exactly that for ``raw_events``) is what reclaims it.
* This runs before the code stops writing the column, because the deploy order
  is migrate-then-roll. An old pod writing ``payload`` fails loudly with
  ``column "payload" of relation "qsos" does not exist`` rather than silently
  filling the column with defaults; the collector logs and continues, so
  ingestion keeps running while the rollout completes.

The reverse comes back as an empty column. That is acceptable because the data
still exists in ``raw_events`` and in the object-storage archive; it is recorded
here so nobody assumes a rollback restores the values.
"""

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("dashboard", "0010_maintenance_run_compact"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="qso",
            name="payload",
        ),
    ]
