from django.contrib import admin
from django.urls import include, path
from dashboard import admin_views


urlpatterns = [
    path("administration/", admin_views.maintenance, name="admin-maintenance"),
    path("administration/data-quality/", admin_views.data_quality, name="admin-data-quality"),
    path("administration/update-talkgroups/", admin_views.update_talkgroups, name="admin-update-talkgroups"),
    path("administration/rebuild-qsos/", admin_views.rebuild_qsos, name="admin-rebuild-qsos"),
    path("administration/irrelevant-raw-events/", admin_views.clear_irrelevant_raw_events, name="admin-clear-irrelevant"),
    path("administration/raw-events/<int:months>/", admin_views.clear_raw_events, name="admin-clear-raw-events"),
    path("administration/counts/<str:kind>/<int:months>/", admin_views.retention_counts, name="admin-retention-counts"),
    path("administration/qsos/<int:months>/", admin_views.clear_qsos, name="admin-clear-qsos"),
    path("administration/users/<int:user_id>/expire-sessions/", admin_views.expire_sessions, name="admin-expire-sessions"),
    path("admin/", admin.site.urls),
    path("", include("dashboard.urls")),
]
