"""Compatibility exports for integrations that imported the original module."""

from .auth_views import register
from .core_views import dashboard, dashboard_api, qsos_api, talkgroups_api
from .live_views import live_qsos, live_qsos_api
from .public_views import health, status
from .report_views import reports

__all__ = [
    "dashboard", "dashboard_api", "qsos_api", "talkgroups_api", "live_qsos",
    "live_qsos_api", "reports", "register", "health", "status",
]
