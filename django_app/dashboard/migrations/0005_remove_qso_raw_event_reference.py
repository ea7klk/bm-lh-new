from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("dashboard", "0004_remove_non_qso_destinations")]

    operations = [
        migrations.RunSQL(
            """
            DO $$
            DECLARE constraint_name text;
            BEGIN
                FOR constraint_name IN
                    SELECT conname
                    FROM pg_constraint
                    WHERE conrelid = 'qsos'::regclass
                      AND confrelid = 'raw_events'::regclass
                LOOP
                    EXECUTE format('ALTER TABLE qsos DROP CONSTRAINT %I', constraint_name);
                END LOOP;
            END $$;
            """,
            migrations.RunSQL.noop,
        ),
        migrations.RemoveField(
            model_name="qso",
            name="raw_event_id",
        ),
    ]
