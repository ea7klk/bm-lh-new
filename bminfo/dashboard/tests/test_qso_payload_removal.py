"""The QSO row must not carry a copy of the raw event.

A `payload` JSONField used to sit on `QSO`, holding the complete BrandMeister
event that produced the QSO. Measured in production before removal: 760 bytes
per row, 2,668 MB across 3.68M rows, 71.7% of the table's heap — and a
byte-identical duplicate of `raw_events.payload`, which the archiver already
ships to object storage.

These tests pin the removal at the three places that matter: the model, the
write paths, and the recovery path. They are cheap and they fail loudly if the
field is reintroduced by a future migration that regenerates the model.
"""

import pathlib

from django.test import SimpleTestCase

from dashboard.models import QSO, RawEvent

ROOT = pathlib.Path(__file__).resolve().parents[3]
COLLECTOR = ROOT / "bminfo" / "dashboard" / "collector.py"
REBUILD = ROOT / "bminfo" / "dashboard" / "rebuild.py"


class PayloadRemovalTests(SimpleTestCase):
    def test_the_qso_model_has_no_payload_field(self):
        field_names = {field.name for field in QSO._meta.get_fields()}
        self.assertNotIn("payload", field_names)

    def test_the_raw_event_keeps_its_payload(self):
        """The raw event is the system of record; recovery depends on it."""
        field_names = {field.name for field in RawEvent._meta.get_fields()}
        self.assertIn("payload", field_names)

    def test_the_migration_dropped_the_column(self):
        migrations = sorted(
            (ROOT / "bminfo" / "dashboard" / "migrations").glob("001*_*.py")
        )
        text = "\n".join(path.read_text() for path in migrations)
        self.assertIn("name=\"payload\"", text)
        self.assertIn("RemoveField", text)

    def test_the_collector_does_not_write_a_qso_payload(self):
        source = COLLECTOR.read_text()
        # The raw event still stores the payload; the QSO must not.
        self.assertNotIn(
            'QSO.objects.bulk_create',
            source.split("payload=payload")[0].rsplit("return", 1)[-1],
            "the collector appears to pass payload into the QSO upsert",
        )
        # The upsert's column list is the precise statement we care about.
        upsert = source.split("QSO.objects.bulk_create", 1)[1]
        update_fields = upsert.split("update_fields=[", 1)[1].split("]", 1)[0]
        self.assertNotIn('"payload"', update_fields)

    def test_the_rebuild_does_not_write_a_qso_payload(self):
        source = REBUILD.read_text()
        upsert = source.split("QSO.objects.bulk_create", 1)[1]
        update_fields = upsert.split("update_fields=[", 1)[1].split("]", 1)[0]
        self.assertNotIn('"payload"', update_fields)

    def test_the_rebuild_still_reads_payloads_from_the_sources(self):
        """Dropping the QSO copy must not have touched the recovery path.

        The rebuild takes its input from raw_events (and the archive); if this
        stops being true, removing the QSO column would have destroyed the only
        remaining copy in the database.
        """
        source = REBUILD.read_text()
        self.assertIn('"payload")', source)
        self.assertIn("iter_archived_payloads", source)
