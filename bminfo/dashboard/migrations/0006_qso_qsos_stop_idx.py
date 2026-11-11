from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0005_remove_qso_raw_event_reference")]

    operations = [
        # The live-QSO consumer asks for "the last 30 minutes" once per second
        # per open page, ordered by stop_at. Without this index PostgreSQL
        # answers it by scanning every QSO row: 859 ms on production at 3.6M
        # rows. With it, the same query is an index scan of 0.14 ms.
        migrations.AddIndex(
            model_name="qso",
            index=models.Index(fields=["-stop_at"], name="qsos_stop_idx"),
        ),
    ]
