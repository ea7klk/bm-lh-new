from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0003_remove_qso_raw_event_fk")]

    operations = [
        migrations.RunSQL(
            "DELETE FROM qsos WHERE destination_id IN (4000, 9990);",
            migrations.RunSQL.noop,
        ),
    ]
