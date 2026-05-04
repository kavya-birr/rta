"""Django settings for the openreversefeed reference app.

This is a demo-only configuration. It deliberately skips auth and uses SQLite
for Django's session / admin tables so the demo runs without any external
state beyond the library's Postgres. The library itself uses Postgres via
SQLAlchemy — Django never talks to that database directly.
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get(
    "DJANGO_SECRET_KEY", "dev-only-change-me-before-production-use-1234567890"
)
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"

# In production we trust only the hostname Render assigns us (auto-injected
# as RENDER_EXTERNAL_HOSTNAME) plus any custom domain set via env. In dev,
# accept everything for convenience.
if DEBUG:
    ALLOWED_HOSTS = ["*"]
else:
    ALLOWED_HOSTS = [
        h for h in [
            os.environ.get("RENDER_EXTERNAL_HOSTNAME"),
            os.environ.get("CUSTOM_DOMAIN"),
            "localhost",
            "127.0.0.1",
        ] if h
    ]

# Production-only security headers — turned on whenever DEBUG is False.
if not DEBUG:
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    # Render terminates TLS at its edge proxy and forwards http to our
    # process. Trust the X-Forwarded-Proto header so Django knows the
    # original request was https (otherwise SECURE_SSL_REDIRECT loops).
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    # Prevent clickjacking, sniffing, etc.
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = "DENY"
    # Tell browsers to keep using HTTPS for 1 year (HSTS)
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 365
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    # CSRF: trust the Render-issued domain
    CSRF_TRUSTED_ORIGINS = [
        f"https://{h}" for h in ALLOWED_HOSTS if h not in ("localhost", "127.0.0.1")
    ]

# Applications
INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.staticfiles",
    "django.contrib.messages",
    "uploads.apps.UploadsConfig",
    "corrections.apps.CorrectionsConfig",
    "dashboard.apps.DashboardConfig",
    "clients.apps.ClientsConfig",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Whitenoise serves /static/ files efficiently from the gunicorn process —
    # no need for nginx/CDN config on Render. Must sit immediately after
    # SecurityMiddleware to handle compressed static delivery.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

ROOT_URLCONF = "reference_app.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.contrib.messages.context_processors.messages",
                "reference_app.context.nav_counts",
            ],
        },
    },
]

WSGI_APPLICATION = "reference_app.wsgi.application"

# Data directory: where stateful files live (JSON stores, AMFI cache,
# uploaded feed files, sqlite when used). On Render free tier this is /tmp/ofr
# (ephemeral). On Starter+ this is /data (persistent disk).
DATA_DIR = Path(os.environ.get("OFR_DATA_DIR", BASE_DIR))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Django's own DB (auth, sessions, messages). When DATABASE_URL is set
# (Render injects it), use Postgres so state survives restarts. Otherwise
# fall back to sqlite at DATA_DIR for local dev.
_database_url = os.environ.get("DATABASE_URL", "")
if _database_url:
    # Parse the postgres:// URL into Django's DATABASES dict format.
    # Render's connectionString comes in like:
    #   postgres://user:pass@host:port/dbname
    # urlparse needs a recognised scheme — accept both postgres:// and
    # postgresql:// transparently.
    from urllib.parse import urlparse
    _normalised = _database_url
    if _normalised.startswith("postgres://"):
        _normalised = _normalised.replace("postgres://", "postgresql://", 1)
    u = urlparse(_normalised)
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": (u.path or "/").lstrip("/"),
            "USER": u.username or "",
            "PASSWORD": u.password or "",
            "HOST": u.hostname or "",
            "PORT": str(u.port) if u.port else "",
            "CONN_MAX_AGE": 60,
            # `prefer` works whether or not the server supports SSL — Render's
            # internal Postgres URLs (`dpg-xxx-a`) don't always advertise SSL
            # on free tier, while external URLs (`*.render.com`) require it.
            # Using `prefer` covers both cleanly.
            "OPTIONS": {"sslmode": "prefer"} if u.hostname and "localhost" not in u.hostname else {},
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": DATA_DIR / "django_internal.sqlite3",
        }
    }

LANGUAGE_CODE = "en-in"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = False
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
# Whitenoise: gzip + brotli compression for static files. We use the
# non-manifest variant — manifest storage hashes every referenced asset
# and fails the build if any reference is broken, which is too strict
# for our minimal-static setup (we only have admin & whitenoise's own
# files).
STATICFILES_STORAGE = "whitenoise.storage.CompressedStaticFilesStorage"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Allow large feed file uploads (default 2.5 MB is too small for real files).
DATA_UPLOAD_MAX_MEMORY_SIZE = 50 * 1024 * 1024  # 50 MB
FILE_UPLOAD_MAX_MEMORY_SIZE = 50 * 1024 * 1024  # 50 MB

# Where uploaded feed files land on disk. The worker polls this directory.
UPLOAD_DIR = Path(os.environ.get("OFR_UPLOAD_DIR", DATA_DIR / "uploaded_files"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Library database URL (also read by openreversefeed.settings directly).
OFR_DATABASE_URL = os.environ.get(
    "OFR_DATABASE_URL", "postgresql+psycopg://ofr:ofr@localhost:5438/ofr"
)
os.environ.setdefault("OFR_DATABASE_URL", OFR_DATABASE_URL)
