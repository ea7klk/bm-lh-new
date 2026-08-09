from __future__ import annotations

from datetime import timedelta

from django.db import connection, connections
from django.utils import timezone

from .models import RawEvent
from .qso_rules import raw_event_threshold_seconds


def clear_irrelevant_raw_events():
    threshold = raw_event_threshold_seconds(); candidates = []; seen = set()
    for raw in RawEvent.objects.order_by("session_id", "event_type", "-received_at").iterator(chunk_size=2000):
        key = (raw.session_id, raw.event_type.lower()); duplicate = key in seen; seen.add(key)
        below = raw.start_at and raw.stop_at and (raw.stop_at - raw.start_at).total_seconds() < threshold
        if raw.event_type.lower() != "session-stop" or below or duplicate:
            candidates.append(raw.pk)
    deleted, _ = RawEvent.objects.filter(pk__in=candidates).delete()
    with connection.cursor() as cursor: cursor.execute("VACUUM (ANALYZE, PARALLEL 0) raw_events")
    return deleted


def nightly_cleanup(stop_event):
    ran_on = None
    while not stop_event.is_set():
        now = timezone.localtime()
        if now.hour == 2 and ran_on != now.date():
            try:
                clear_irrelevant_raw_events()
            except Exception:
                import logging
                logging.getLogger(__name__).exception("nightly raw-event cleanup failed")
            finally:
                # This worker is outside Django's request lifecycle and must
                # release its database connection explicitly.
                connections.close_all()
            ran_on = now.date()
        stop_event.wait(30)
