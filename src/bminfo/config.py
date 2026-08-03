from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import quote


def _database_url() -> str:
    """Build the PostgreSQL DSN from components, retaining an optional legacy override."""
    explicit_url = os.getenv("DATABASE_URL", "").strip()
    if explicit_url:
        return explicit_url

    username = quote(os.getenv("POSTGRES_USER", "bminfo"), safe="")
    password = quote(os.getenv("POSTGRES_PASSWORD", "bminfo"), safe="")
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    database = quote(os.getenv("POSTGRES_DB", "bminfo"), safe="")
    return f"postgresql://{username}:{password}@{host}:{port}/{database}"


@dataclass(frozen=True)
class Settings:
    database_url: str = _database_url()
    bm_url: str = os.getenv("BM_URL", "https://api.brandmeister.network")
    bm_socketio_path: str = os.getenv("BM_SOCKETIO_PATH", "/lh/socket.io")
    bm_join: str = os.getenv("BM_JOIN", "everything")
    talkgroups_url: str = os.getenv(
        "TALKGROUPS_URL", "https://api.brandmeister.network/v2/talkgroup"
    )
    talkgroups_sync_hours: float = float(os.getenv("TALKGROUPS_SYNC_HOURS", "24"))
    exclude_local_talkgroup: int = int(os.getenv("EXCLUDE_LOCAL_TALKGROUP", "9"))
    kerchunk_threshold_seconds: float = float(
        os.getenv("KERCHUNK_THRESHOLD_SECONDS", "3")
    )
    collector_heartbeat_seconds: int = int(
        os.getenv("COLLECTOR_HEARTBEAT_SECONDS", "30")
    )
    app_host: str = os.getenv("APP_HOST", "0.0.0.0")
    app_port: int = int(os.getenv("APP_PORT", "8000"))
    app_timezone: str = os.getenv("APP_TIMEZONE", "Europe/Madrid")
    admin_password: str = os.getenv("ADMIN_PASSWORD", "")
    cookie_secure: bool = os.getenv("COOKIE_SECURE", "false").lower() == "true"
    session_hours: int = int(os.getenv("SESSION_HOURS", "168"))
    postgres_pool_min_size: int = int(os.getenv("POSTGRES_POOL_MIN_SIZE", "2"))
    postgres_pool_max_size: int = int(os.getenv("POSTGRES_POOL_MAX_SIZE", "20"))
    matomo_enabled: bool = os.getenv("MATOMO_ENABLED", "false").lower() == "true"
    matomo_url: str = os.getenv("MATOMO_URL", "")
    matomo_site_id: str = os.getenv("MATOMO_SITE_ID", "")
    app_public_url: str = os.getenv("APP_PUBLIC_URL", "http://localhost:8000").rstrip("/")
    smtp_enabled: bool = os.getenv("SMTP_ENABLED", "true").lower() == "true"
    smtp_host: str = os.getenv("SMTP_HOST", "mail.conxtor.com")
    smtp_port: int = int(os.getenv("SMTP_PORT", "587"))
    smtp_username: str = os.getenv("SMTP_USERNAME", "bm-lh@ea7klk.es")
    smtp_password: str = os.getenv("SMTP_PASSWORD", "")
    smtp_use_tls: bool = os.getenv("SMTP_USE_TLS", "true").lower() == "true"
    smtp_use_ssl: bool = os.getenv("SMTP_USE_SSL", "false").lower() == "true"
    smtp_from_email: str = os.getenv("SMTP_FROM_EMAIL", "bm-lh@ea7klk.es")
    smtp_from_name: str = os.getenv("SMTP_FROM_NAME", "BrandMeister Lastheard")
    smtp_reply_to: str = os.getenv("SMTP_REPLY_TO", "bm-lh@ea7klk.es")
    smtp_timeout_seconds: int = int(os.getenv("SMTP_TIMEOUT_SECONDS", "20"))
    email_verification_hours: int = int(os.getenv("EMAIL_VERIFICATION_HOURS", "48"))
    password_reset_hours: int = int(os.getenv("PASSWORD_RESET_HOURS", "1"))


settings = Settings()
