from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from datetime import timedelta

from .core_views import qso_json
from .models import QSO
from .utils import LIVE_WINDOW_MINUTES


@login_required
def live_qsos(request):
    return render(request, "dashboard/live_qsos.html")


@login_required
def live_qsos_api(request):
    # Same trailing window as the websocket consumer; both read
    # LIVE_WINDOW_MINUTES so the two live endpoints cannot disagree.
    start = timezone.now() - timedelta(minutes=LIVE_WINDOW_MINUTES)
    rows = QSO.objects.filter(stop_at__gte=start).select_related("talkgroup").order_by("-stop_at")[:100]
    return JsonResponse([qso_json(row, relative=True) for row in rows], safe=False)
