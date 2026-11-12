from django.db import migrations


class Migration(migrations.Migration):
    """Keep the visibility map fresh enough for qsos_cover_idx to be used.

    An Index Only Scan only skips the heap for pages marked all-visible. With
    the default autovacuum_vacuum_scale_factor of 0.2 this table needs about
    727,000 dead tuples before autovacuum runs - roughly 24 days at current
    write rates. In between, PostgreSQL reports the plan as an Index Only Scan
    but performs a heap fetch for every row: measured on production, 249,648
    heap fetches and a 488 ms scan, against 96 fetches and 24 ms immediately
    after a VACUUM.

    Lowering the scale factor brings autovacuum round every few days, so the
    index keeps its benefit instead of decaying between maintenance windows.
    """

    dependencies = [("dashboard", "0007_qso_covering_index")]

    operations = [
        migrations.RunSQL(
            sql="""
                ALTER TABLE qsos SET (
                    autovacuum_vacuum_scale_factor = 0.02,
                    autovacuum_analyze_scale_factor = 0.05
                );
            """,
            reverse_sql="""
                ALTER TABLE qsos RESET (
                    autovacuum_vacuum_scale_factor,
                    autovacuum_analyze_scale_factor
                );
            """,
        ),
    ]
