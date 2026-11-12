from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0006_qso_qsos_stop_idx")]

    operations = [
        # Covers the dashboard's range scan: start_at as the key plus exactly the
        # five columns dashboard_stats selects. Without source_name the planner
        # ignores this index and keeps using qsos_start_idx, so all four INCLUDE
        # columns are needed for it to be chosen at all.
        #
        # Costs about 200 MB and a few seconds to build. It also makes
        # qsos_start_idx redundant for every query it serves; that index is left
        # in place for now to keep this change reversible.
        migrations.AddIndex(
            model_name="qso",
            index=models.Index(
                fields=["-start_at"],
                name="qsos_cover_idx",
                include=["source_call", "source_name", "talkgroup", "duration_ms"],
            ),
        ),
    ]
