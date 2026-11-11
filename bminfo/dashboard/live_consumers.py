import asyncio
from datetime import timedelta

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.db.models import Q
from django.utils import timezone

from .core_views import qso_json
from .models import QSO
from .utils import LIVE_WINDOW_MINUTES


def _window_start(now=None):
    return (now or timezone.now()) - timedelta(minutes=LIVE_WINDOW_MINUTES)


def replay_order(rows):
    """Order a delta batch the way the page must receive it.

    The page prepends every delta and keeps the array it was given, so a batch
    has to arrive oldest-first for the newest QSO to end up on top. rows()
    returns newest-first for the snapshot, so the batch is reversed here.
    """
    return list(reversed(rows))


class LiveQSOConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        if not self.scope["user"].is_authenticated:
            await self.close(code=4401)
            return
        await self.accept()
        self.filters = {}
        self.last_seen = _window_start()
        await self.send_snapshot()
        self.stream_task = asyncio.create_task(self.stream())

    async def disconnect(self, close_code):
        if hasattr(self, "stream_task"):
            self.stream_task.cancel()

    async def receive_json(self, content, **kwargs):
        if content.get("type") in {"subscribe", "filters"}:
            self.filters = content.get("filters", content)
            self.last_seen = _window_start()
            await self.send_snapshot()

    async def send_snapshot(self):
        # Newest first, which is the order the page renders: the client keeps
        # the array as received.
        rows = await self.rows(after=None)
        self.last_seen = max((row["_stop"] for row in rows), default=_window_start())
        for row in rows:
            row.pop("_stop", None)
        await self.send_json({"type": "snapshot", "qsos": rows})

    async def stream(self):
        try:
            while True:
                await asyncio.sleep(1)
                rows = await self.rows(after=self.last_seen)
                for row in replay_order(rows):
                    self.last_seen = max(self.last_seen, row["_stop"])
                    row.pop("_stop", None)
                    await self.send_json({"type": "qso", "qso": row})
        except asyncio.CancelledError:
            return

    @database_sync_to_async
    def rows(self, after=None):
        result = []
        for qso in self.queryset(after):
            item = qso_json(qso, relative=True); item["_stop"] = qso.stop_at; result.append(item)
        return result

    def queryset(self, after=None):
        """The 100 newest QSOs inside the live window, newest first.

        Separate from rows() so the window and ordering rules are testable
        without an event loop. Ordering descending keeps the newest 100 rather
        than the oldest 100 when the window holds more than the cap.
        """
        filters = Q(stop_at__gte=_window_start())
        if after is not None:
            filters &= Q(stop_at__gt=after)
        callsign = str(self.filters.get("callsign", "")).strip()
        if callsign:
            filters &= Q(source_call__icontains=callsign)
        continent = self.filters.get("continent")
        country = self.filters.get("country")
        if continent and continent != "All": filters &= Q(talkgroup__continent=continent)
        if country and country != "All": filters &= Q(talkgroup__country=country)
        talkgroups = [int(value) for value in self.filters.get("talkgroups", []) if str(value).isdigit()]
        if talkgroups: filters &= Q(talkgroup_id__in=talkgroups)
        return QSO.objects.filter(filters).select_related("talkgroup").order_by("-stop_at")[:100]
