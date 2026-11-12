from pathlib import Path
import os


BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


SECRET_KEY = os.getenv("APP_SECRET_KEY") or os.getenv("ADMIN_PASSWORD", "unsafe-development-key")
DEBUG = env_bool("APP_DEBUG", False)
ALLOWED_HOSTS = [host.strip() for host in os.getenv("APP_ALLOWED_HOSTS", "*").split(",") if host.strip()]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "channels",
    "dashboard",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "dashboard.context_processors.app_context",
            ],
        },
    }
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("DB_NAME", "bminfo"),
        "USER": os.getenv("DB_USER", "bminfo"),
        "PASSWORD": os.getenv("DB_PASSWORD", "bminfo"),
        "HOST": os.getenv("DB_HOST", "postgres"),
        "PORT": os.getenv("DB_PORT", "5432"),
        # Keep connections reusable for a bounded period. Django closes stale
        # connections at request boundaries, while the finite lifetime avoids
        # accumulating idle connections across worker and collector threads.
        "CONN_MAX_AGE": int(os.getenv("DB_CONN_MAX_AGE", "60")),
        "CONN_HEALTH_CHECKS": True,
    }
}

AUTH_USER_MODEL = "dashboard.User"
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/"
LOGOUT_REDIRECT_URL = "/"
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
    "django.contrib.auth.hashers.ScryptPasswordHasher",
]

LANGUAGE_CODE = os.getenv("APP_LANGUAGE_CODE", "en-us")
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = env_bool("COOKIE_SECURE", False)
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
CSRF_TRUSTED_ORIGINS = [
    value.strip()
    for value in os.getenv("APP_CSRF_TRUSTED_ORIGINS", "").split(",")
    if value.strip()
]

DATABASE_ROUTERS = []

SMTP_ENABLED = env_bool("SMTP_ENABLED", True)
EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
EMAIL_HOST = os.getenv("SMTP_HOST", "mail.conxtor.com")
EMAIL_PORT = int(os.getenv("SMTP_PORT", "587"))
EMAIL_HOST_USER = os.getenv("SMTP_USERNAME", "bm-lh@ea7klk.es")
EMAIL_HOST_PASSWORD = os.getenv("SMTP_PASSWORD", "")
EMAIL_USE_TLS = env_bool("SMTP_USE_TLS", True)
EMAIL_USE_SSL = env_bool("SMTP_USE_SSL", False)
EMAIL_TIMEOUT = int(os.getenv("SMTP_TIMEOUT_SECONDS", "20"))
DEFAULT_FROM_EMAIL = os.getenv("SMTP_FROM_EMAIL", EMAIL_HOST_USER)
SMTP_FROM_NAME = os.getenv("SMTP_FROM_NAME", "BrandMeister Statistics by EA7KLK")
SMTP_REPLY_TO = os.getenv("SMTP_REPLY_TO", "")
SERVER_EMAIL = DEFAULT_FROM_EMAIL
APP_PUBLIC_URL = os.getenv("APP_PUBLIC_URL", "http://localhost:8000").rstrip("/")
EMAIL_VERIFICATION_HOURS = int(os.getenv("EMAIL_VERIFICATION_HOURS", "48"))
UNVERIFIED_USER_RETENTION_DAYS = int(os.getenv("UNVERIFIED_USER_RETENTION_DAYS", "5"))
PASSWORD_RESET_HOURS = int(os.getenv("PASSWORD_RESET_HOURS", "1"))
# Per-transaction work_mem for the dashboard aggregate. Its two
# count(DISTINCT) sorts spill about 10 MB each at the 8 MB default.
# Set to an empty string to inherit the server setting.
DASHBOARD_WORK_MEM = os.getenv("DASHBOARD_WORK_MEM", "64MB")

# Raw-event archiving to the Silo object store (S3-compatible).
# Archiving only runs when an endpoint, bucket and both keys are present; the
# collector logs why it is idle otherwise. See OBJECT-STORAGE.md.
S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL", "").rstrip("/")
S3_BUCKET = os.getenv("S3_BUCKET", "bminfo-raw")
S3_ACCESS_KEY_ID = os.getenv("S3_ACCESS_KEY_ID", "")
S3_SECRET_ACCESS_KEY = os.getenv("S3_SECRET_ACCESS_KEY", "")
S3_REGION = os.getenv("S3_REGION", "us-east-1")
S3_ADDRESSING_STYLE = os.getenv("S3_ADDRESSING_STYLE", "path")
RAW_ARCHIVE_PREFIX = os.getenv("RAW_ARCHIVE_PREFIX", "raw-events").strip("/")
# How often the background loop looks for archivable days.
RAW_ARCHIVE_INTERVAL_SECONDS = int(os.getenv("RAW_ARCHIVE_INTERVAL_SECONDS", "3600"))
# Set to false to keep archiving manual (the admin button and command still work).
RAW_ARCHIVE_ENABLED = env_bool("RAW_ARCHIVE_ENABLED", True)
# Reclaim disk after a pass that evicted days. DELETE frees pages for reuse but
# never shrinks the file; VACUUM FULL does, at the cost of an ACCESS EXCLUSIVE
# lock while it rewrites the table. Runs at most once per archive pass, and only
# when the pass deleted at least RAW_COMPACT_MIN_ROWS rows.
RAW_ARCHIVE_COMPACT = env_bool("RAW_ARCHIVE_COMPACT", True)
RAW_COMPACT_MIN_ROWS = int(os.getenv("RAW_COMPACT_MIN_ROWS", "20000"))
RAW_COMPACT_LOCK_TIMEOUT_SECONDS = int(os.getenv("RAW_COMPACT_LOCK_TIMEOUT_SECONDS", "30"))
MATOMO_ENABLED = env_bool("MATOMO_ENABLED", False)
MATOMO_URL = os.getenv("MATOMO_URL", "")
MATOMO_SITE_ID = os.getenv("MATOMO_SITE_ID", "")
