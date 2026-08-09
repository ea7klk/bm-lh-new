from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import (
    EmailChangeRequest,
    EmailVerificationToken,
    PasswordResetToken,
    QSO,
    RawEvent,
    ServiceHeartbeat,
    Talkgroup,
    User,
    UserSession,
)


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    ordering = ("callsign",)
    list_display = ("callsign", "name", "email", "is_active", "is_staff", "last_login_at")
    search_fields = ("callsign", "name", "email")
    list_filter = ("is_active", "is_staff", "email_verified_at")
    fieldsets = (
        (None, {"fields": ("callsign", "password")} ),
        ("Personal information", {"fields": ("name", "email")} ),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")} ),
        ("Dates", {"fields": ("created_at", "last_login_at", "email_verified_at")} ),
    )
    add_fieldsets = (
        (None, {"classes": ("wide",), "fields": ("callsign", "email", "name", "password1", "password2", "is_staff", "is_superuser")} ),
    )


@admin.register(RawEvent)
class RawEventAdmin(admin.ModelAdmin):
    list_display = ("id", "event_type", "session_id", "received_at", "start_at", "stop_at")
    list_filter = ("event_type", "received_at")
    search_fields = ("session_id", "payload_hash")
    date_hierarchy = "received_at"
    readonly_fields = ("payload_hash", "payload")
    list_per_page = 100


@admin.register(QSO)
class QSOAdmin(admin.ModelAdmin):
    list_display = ("session_id", "source_call", "talkgroup", "start_at", "duration_ms", "slot")
    list_filter = ("slot", "start_at", "talkgroup__continent", "talkgroup__country")
    search_fields = ("session_id", "source_call", "source_name", "destination_name")
    date_hierarchy = "start_at"
    list_per_page = 100


@admin.register(Talkgroup)
class TalkgroupAdmin(admin.ModelAdmin):
    list_display = ("talkgroup_id", "name", "continent", "full_country_name", "last_updated")
    list_filter = ("continent", "country")
    search_fields = ("name", "talkgroup_id", "full_country_name")


@admin.register(ServiceHeartbeat)
class ServiceHeartbeatAdmin(admin.ModelAdmin):
    list_display = ("service_name", "last_seen_at")
    readonly_fields = ("service_name", "last_seen_at")


for model in (UserSession, EmailVerificationToken, PasswordResetToken, EmailChangeRequest):
    admin.site.register(model)


admin.site.site_header = "BrandMeister Lastheard administration"
admin.site.site_title = "BrandMeister Lastheard admin"
admin.site.index_title = "Application data and operations"
