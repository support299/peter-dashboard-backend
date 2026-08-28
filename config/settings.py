"""
Django settings for config project.
"""

from pathlib import Path
from urllib.parse import urlparse
from dotenv import load_dotenv
import os

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.getenv(
    "SECRET_KEY",
    "",
)

DEBUG = os.getenv("DJANGO_DEBUG", "true").lower() == "true"

_jobber_redirect = os.getenv("JOBBER_CLIENT_REDIRECT_URI", "")
_jobber_redirect_host = urlparse(_jobber_redirect).hostname
_frontend_url = os.getenv("FRONTEND_URL", "http://localhost:5173")
_frontend_host = urlparse(_frontend_url).hostname

ALLOWED_HOSTS = [
    "localhost",
    "127.0.0.1",
    ".ngrok-free.dev",
    ".ngrok.io",
    ".ngrok.app",
]
if _jobber_redirect_host:
    ALLOWED_HOSTS.append(_jobber_redirect_host)
if _frontend_host:
    ALLOWED_HOSTS.append(_frontend_host)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "corsheaders",
    "django_celery_beat",
    "django_celery_results",
    "integrations",
    "operations",
    "analytics",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.getenv("DB_NAME"),
        "USER": os.getenv("DB_USER"),
        "PASSWORD": os.getenv("DB_PASSWORD"),
        "HOST": os.getenv("DB_HOST"),
        "PORT": os.getenv("DB_PORT"),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

MAILERS = {
    "default": {
        "BACKEND": "django.core.mail.backends.console.EmailBackend",
    },
}

CORS_ALLOWED_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:4173",
    "http://127.0.0.1:4173",
    "http://51.21.79.241",
]
CORS_ALLOW_CREDENTIALS = True

CSRF_TRUSTED_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "http://51.21.79.241",
]
if _jobber_redirect.startswith("https://"):
    CSRF_TRUSTED_ORIGINS.append(f"{urlparse(_jobber_redirect).scheme}://{urlparse(_jobber_redirect).netloc}")
if _frontend_url.startswith("http"):
    CSRF_TRUSTED_ORIGINS.append(_frontend_url.rstrip("/"))

FRONTEND_URL = _frontend_url.rstrip("/")

ADMIN_INTERNAL_APP = {
    "API_KEY": os.getenv("ADMIN_INTERNAL_APP_API_KEY", ""),
    "BASE_URL": os.getenv("ADMIN_INTERNAL_APP_BASE_URL", "http://127.0.0.1:8000"),
    "ANALYTICS_PATH": os.getenv(
        "ADMIN_INTERNAL_APP_ANALYTICS_PATH",
        "/api/admin-internal-app/analytics/",
    ),
    "TIMEOUT": int(os.getenv("ADMIN_INTERNAL_APP_TIMEOUT", "60")),
}

PRICING_CALCULATOR_APP = {
    "API_KEY": os.getenv("PRICING_CALCULATOR_APP_API_KEY", ""),
    "BASE_URL": os.getenv("PRICING_CALCULATOR_APP_BASE_URL", "http://127.0.0.1:8002"),
    "ANALYTICS_PATH": os.getenv(
        "PRICING_CALCULATOR_APP_PRICING_CALCULATOR_PATH",
        os.getenv(
            "PRICING_CALCULATOR_APP_ANALYTICS_PATH",
            "/api/pricing-calculator/analytics/",
        ),
    ),
    "TIMEOUT": int(os.getenv("PRICING_CALCULATOR_APP_TIMEOUT", "90")),
}


def _jobber_webhook_url():
    explicit = os.getenv("JOBBER_WEBHOOK_URL", "").strip()
    if explicit:
        return explicit if explicit.endswith("/") else f"{explicit}/"
    if not _jobber_redirect:
        return ""
    url = _jobber_redirect.replace("/callback/", "/webhooks/").replace("/callback", "/webhooks")
    return url if url.endswith("/") else f"{url}/"

JOBBER = {
    "CLIENT_ID": os.getenv("JOBBER_CLIENT_ID", ""),
    "CLIENT_SECRET": os.getenv("JOBBER_CLIENT_SECRET", ""),
    "REDIRECT_URI": os.getenv("JOBBER_CLIENT_REDIRECT_URI", ""),
    "SCOPES": os.getenv("JOBBER_SCOPES", "").strip('"').strip("'"),
    "AUTH_URL": os.getenv("JOBBER_AUTH_URL", "https://api.getjobber.com/api/oauth/authorize"),
    "TOKEN_URL": os.getenv("JOBBER_TOKEN_URL", "https://api.getjobber.com/api/oauth/token"),
    "API_URL": os.getenv("JOBBER_API_URL", "https://api.getjobber.com/api/graphql"),
    "GRAPHQL_VERSION": os.getenv("JOBBER_GRAPHQL_VERSION", "2025-04-16"),
    "PAGE_SIZE": int(os.getenv("JOBBER_PAGE_SIZE", "25")),
    "WEBHOOK_URL": _jobber_webhook_url(),
}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {"class": "logging.StreamHandler"},
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
    "loggers": {
        "integrations": {"level": "INFO"},
        "operations": {"level": "INFO"},
        "analytics": {"level": "INFO"},
        "celery": {"level": "INFO"},
    },
}

# ——— Celery ———
CELERY_BROKER_URL = os.getenv("CELERY_BROKER_URL", "redis://127.0.0.1:6379/0")
CELERY_RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", "django-db")
CELERY_CACHE_BACKEND = "django-cache"
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
CELERY_ENABLE_UTC = True
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 60 * 60
CELERY_TASK_SOFT_TIME_LIMIT = 55 * 60
CELERY_BEAT_SCHEDULER = "django_celery_beat.schedulers:DatabaseScheduler"
CELERY_RESULT_EXTENDED = True

