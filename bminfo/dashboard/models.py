from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.utils import timezone


class UserManager(BaseUserManager):
    def create_user(self, callsign: str, email: str, password: str | None = None, **extra):
        if not callsign:
            raise ValueError("callsign is required")
        if not email:
            raise ValueError("email is required")
        user = self.model(callsign=callsign.upper().strip(), email=self.normalize_email(email), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, callsign: str, email: str, password: str, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        extra.setdefault("is_active", True)
        return self.create_user(callsign, email, password, **extra)


class User(AbstractBaseUser, PermissionsMixin):
    callsign = models.CharField(max_length=16, unique=True)
    name = models.CharField(max_length=120)
    email = models.EmailField(unique=True)
    password = models.CharField(max_length=255, db_column="password_hash")
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)
    last_login_at = models.DateTimeField(null=True, blank=True)
    email_verified_at = models.DateTimeField(null=True, blank=True)

    last_login = models.DateTimeField(null=True, blank=True)
    objects = UserManager()
    USERNAME_FIELD = "callsign"
    REQUIRED_FIELDS = ["email"]

    class Meta:
        db_table = "users"
        ordering = ["callsign"]

    def __str__(self):
        return self.callsign


class RawEvent(models.Model):
    payload_hash = models.CharField(max_length=64, unique=True)
    session_id = models.CharField(max_length=255)
    event_type = models.CharField(max_length=80)
    received_at = models.DateTimeField(default=timezone.now)
    start_at = models.DateTimeField(null=True, blank=True)
    stop_at = models.DateTimeField(null=True, blank=True)
    payload = models.JSONField(default=dict)

    class Meta:
        db_table = "raw_events"
        ordering = ["-received_at"]
        indexes = [
            models.Index(fields=["-received_at"], name="raw_events_received_idx"),
            models.Index(fields=["session_id"], name="raw_events_session_idx"),
        ]

    def __str__(self):
        return f"{self.event_type} {self.session_id}"


class Talkgroup(models.Model):
    talkgroup_id = models.BigIntegerField(unique=True)
    name = models.CharField(max_length=255)
    country = models.CharField(max_length=8, default="XX")
    continent = models.CharField(max_length=80, default="Other")
    full_country_name = models.CharField(max_length=120, default="Other")
    last_updated = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "talkgroups"
        ordering = ["talkgroup_id"]
        indexes = [models.Index(fields=["country"], name="talkgroups_country_idx"), models.Index(fields=["continent"], name="talkgroups_continent_idx")]

    def __str__(self):
        return f"{self.name} ({self.talkgroup_id})"


class QSO(models.Model):
    session_id = models.CharField(max_length=255, primary_key=True)
    source_id = models.BigIntegerField(null=True, blank=True)
    source_call = models.CharField(max_length=32, null=True, blank=True)
    source_name = models.CharField(max_length=255, null=True, blank=True)
    talkgroup = models.ForeignKey(
        Talkgroup,
        db_column="destination_id",
        to_field="talkgroup_id",
        on_delete=models.PROTECT,
        related_name="qsos",
        db_constraint=False,
        null=True,
        blank=True,
    )
    destination_call = models.CharField(max_length=32, null=True, blank=True)
    destination_name = models.CharField(max_length=255, null=True, blank=True)
    context_id = models.BigIntegerField(null=True, blank=True)
    link_call = models.CharField(max_length=64, null=True, blank=True)
    link_name = models.CharField(max_length=255, null=True, blank=True)
    link_type_name = models.CharField(max_length=120, null=True, blank=True)
    slot = models.SmallIntegerField(null=True, blank=True)
    master = models.CharField(max_length=255, null=True, blank=True)
    talker_alias = models.CharField(max_length=255, null=True, blank=True)
    rssi = models.FloatField(null=True, blank=True)
    ber = models.FloatField(null=True, blank=True)
    start_at = models.DateTimeField()
    stop_at = models.DateTimeField()
    duration_ms = models.IntegerField()
    is_native_session_id = models.BooleanField(default=True)
    ingested_at = models.DateTimeField(default=timezone.now)
    payload = models.JSONField(default=dict)

    class Meta:
        db_table = "qsos"
        ordering = ["-start_at"]
        indexes = [
            models.Index(fields=["-start_at"], name="qsos_start_idx"),
            models.Index(fields=["source_id", "-start_at"], name="qsos_source_start_idx"),
            models.Index(fields=["talkgroup", "-start_at"], name="qsos_dest_start_idx"),
            # The live-QSO consumer polls a short trailing window once per
            # second per open page, ordered by stop_at. Without this index
            # PostgreSQL answers it with a full scan of every QSO.
            models.Index(fields=["-stop_at"], name="qsos_stop_idx"),
            # Covers the dashboard's range scan. The five columns below are
            # exactly what dashboard_stats selects, so PostgreSQL can answer
            # that scan from the index alone. "talkgroup" is the field; its
            # column is destination_id.
            models.Index(
                fields=["-start_at"],
                name="qsos_cover_idx",
                include=["source_call", "source_name", "talkgroup", "duration_ms"],
            ),
        ]

    def __str__(self):
        return self.session_id


class ServiceHeartbeat(models.Model):
    service_name = models.CharField(max_length=80, primary_key=True)
    last_seen_at = models.DateTimeField()

    class Meta:
        db_table = "service_heartbeats"


class UserSession(models.Model):
    user = models.ForeignKey(User, db_column="user_id", on_delete=models.CASCADE)
    token_hash = models.CharField(max_length=128, unique=True)
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()

    class Meta:
        db_table = "user_sessions"


class EmailVerificationToken(models.Model):
    user = models.ForeignKey(User, db_column="user_id", on_delete=models.CASCADE)
    token_hash = models.CharField(max_length=128, unique=True)
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "email_verification_tokens"


class PasswordResetToken(models.Model):
    user = models.ForeignKey(User, db_column="user_id", on_delete=models.CASCADE)
    token_hash = models.CharField(max_length=128, unique=True)
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "password_reset_tokens"


class EmailChangeRequest(models.Model):
    user = models.ForeignKey(User, db_column="user_id", on_delete=models.CASCADE)
    old_email = models.EmailField()
    new_email = models.EmailField()
    old_token_hash = models.CharField(max_length=128, unique=True)
    new_token_hash = models.CharField(max_length=128, unique=True, null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    old_confirmed_at = models.DateTimeField(null=True, blank=True)
    new_confirmed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "email_change_requests"


class RawEventArchive(models.Model):
    """Record of one UTC day of raw events moved to object storage.

    Rows are only deleted from `raw_events` after the object is uploaded and
    verified, so this row is the authoritative statement that a day now lives
    only in the archive. The archiver consults it to stay idempotent.
    """

    day = models.DateField(unique=True, help_text="UTC day covered by the archive file")
    object_key = models.CharField(max_length=512)
    rows = models.BigIntegerField(default=0)
    bytes_compressed = models.BigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, default="")
    archived_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "raw_event_archives"
        ordering = ["-day"]

    def __str__(self):
        return f"{self.day} ({self.rows} rows)"


class MaintenanceRun(models.Model):
    """Status of a long-running maintenance job started from the admin page.

    The jobs run in worker threads so an admin POST returns immediately; this
    row is how the page reports what happened, and it survives a restart.
    """

    ARCHIVE = "archive"
    REBUILD = "rebuild"
    COMPACT = "compact"
    KIND_CHOICES = [
        (ARCHIVE, "Archive raw events"),
        (REBUILD, "Rebuild QSOs"),
        (COMPACT, "Compact raw events"),
    ]

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STATUS_CHOICES = [(RUNNING, "Running"), (SUCCEEDED, "Succeeded"), (FAILED, "Failed")]

    kind = models.CharField(max_length=16, choices=KIND_CHOICES)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default=RUNNING)
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    message = models.TextField(blank=True, default="")
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "maintenance_runs"
        ordering = ["-started_at"]
        indexes = [models.Index(fields=["kind", "-started_at"], name="maint_runs_kind_idx")]

    def __str__(self):
        return f"{self.kind} {self.status} {self.started_at:%Y-%m-%d %H:%M}"
