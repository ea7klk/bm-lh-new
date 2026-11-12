from django.conf import settings
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.static import serve as static_serve

from dashboard import admin_views


# Design mockups live as plain static files so they can be opened directly from
# the filesystem as well as over HTTP. They contain sample data only.
MOCKUPS_ROOT = settings.BASE_DIR / "mockups"

urlpatterns = [
    path("administration/", admin_views.maintenance, name="admin-maintenance"),
    path("administration/data-quality/", admin_views.data_quality, name="admin-data-quality"),
    path("administration/update-talkgroups/", admin_views.update_talkgroups, name="admin-update-talkgroups"),
    path("administration/rebuild-qsos/", admin_views.rebuild_qsos, name="admin-rebuild-qsos"),
    path("administration/archive/", admin_views.archive_now, name="admin-archive-now"),
    path("administration/archive/status/", admin_views.archive_status, name="admin-archive-status"),
    path("administration/compact/", admin_views.compact_raw_events, name="admin-compact-raw-events"),
    path("administration/counts/<str:kind>/<int:months>/", admin_views.retention_counts, name="admin-retention-counts"),
    path("administration/qsos/<int:months>/", admin_views.clear_qsos, name="admin-clear-qsos"),
    path("administration/users/<int:user_id>/expire-sessions/", admin_views.expire_sessions, name="admin-expire-sessions"),
    path("admin/", admin.site.urls),
    # Static design mockups. django.views.static.serve is acceptable here: it
    # resolves paths with safe_join under a fixed root, refuses directories, and
    # serves read-only HTML and images with no user data.
    re_path(r"^mockups/$", static_serve, {"path": "index.html", "document_root": MOCKUPS_ROOT}, name="mockups-index"),
    re_path(r"^mockups/(?P<path>.*)$", static_serve, {"document_root": MOCKUPS_ROOT}, name="mockups-file"),
    path("", include("dashboard.urls")),
]
