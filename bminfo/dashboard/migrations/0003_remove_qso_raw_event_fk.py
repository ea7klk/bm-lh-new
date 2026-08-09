from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0002_user_last_login")]

    operations = [
        migrations.RenameField(
            model_name="qso",
            old_name="raw_event",
            new_name="raw_event_id",
        ),
        migrations.AlterField(
            model_name="qso",
            name="raw_event_id",
            field=models.BigIntegerField(
                blank=True,
                db_column="raw_event_id",
                null=True,
            ),
        ),
    ]
