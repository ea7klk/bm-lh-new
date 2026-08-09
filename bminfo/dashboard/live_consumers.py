import asyncio
from datetime import timedelta

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncJsonWebsocketConsumer
from django.db.models import Q
from django.utils import timezone

from .core_views import qso_json
from .models import QSO


class LiveQSOConsumer(AsyncJsonWebsocketConsumer):
    async def connect(self):
        if not self.scope["user"].is_authenticated:
            await self.close(code=4401)
            return
        await self.accept()
        self.filters = {}
        self.last_seen = timezone.now() - timedelta(minutes=30)
        await self.send_snapshot()
        self.stream_task = asyncio.create_task(self.stream())

    async def disconnect(self, close_code):
        if hasattr(self, "stream_task"):
            self.stream_task.cancel()

    async def receive_json(self, content, **kwargs):
        if content.get("type") in {"subscribe", "filters"}:
            self.filters = content.get("filters", content)
            self.last_seen = timezone.now() - timedelta(minutes=30)
            await self.send_snapshot()

    async def send_snapshot(self):
        rows = await self.rows(after=None)
        self.last_seen = max((row["_stop"] for row in rows), default=timezone.now() - timedelta(minutes=30))
        for row in rows:
            row.pop("_stop", None)
        await self.send_json({"type": "snapshot", "qsos": rows})

    async def stream(self):
        try:
            while True:
                await asyncio.sleep(1)
                rows = await self.rows(after=self.last_seen)
                for row in rows:
                    self.last_seen = max(self.last_seen, row["_stop"])
                    row.pop("_stop", None)
                    await self.send_json({"type": "qso", "qso": row})
        except asyncio.CancelledError:
            return

    @database_sync_to_async
    def rows(self, after=None):
        start = timezone.now() - timedelta(minutes=30)
        filters = Q(stop_at__gte=start)
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
        result = []
        for qso in QSO.objects.filter(filters).select_related("talkgroup").order_by("stop_at")[:100]:
            item = qso_json(qso, relative=True); item["_stop"] = qso.stop_at; result.append(item)
        return result
