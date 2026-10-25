"""Shared settings for every environment.

Environment-specific modules import ``*`` from here and override what differs.
Deployment-varying values come from the environment via django-environ, never
from hardcoded defaults - see ``.env.example``.
"""

from datetime import timedelta
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, []),
    CORS_ALLOWED_ORIGINS=(list, []),
)
environ.Env.read_env(BASE_DIR / ".env")

# No default: a missing SECRET_KEY must crash loudly rather than silently
# falling back to a shared, guessable value.
SECRET_KEY = env("SECRET_KEY")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")

# ---------------------------------------------------------------------------
# Applications
#
# Local apps are listed in dependency-layer order. Imports point downward only:
# an app may import from a lower layer, never from the same layer or a higher
# one. Data shared by two apps in the same layer belongs one layer down.
# ---------------------------------------------------------------------------
DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    "corsheaders",
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "django_filters",
    "drf_spectacular",
    "auditlog",
]

LOCAL_APPS = [
    "apps.core",  # L1 - base models, managers, shared DRF machinery
    "apps.users",  # L2 - custom user, authentication, RBAC
    "apps.audit",  # L2 - activity-log and system-log read APIs
    "apps.notifications",  # L2 - per-user in-app notifications
    "apps.gems",  # L2 - domain enums and stone reference tables
    "apps.orders",  # L3 - customers, orders and stones
    "apps.billing",  # L4 - bills, payments and the GePG gateway
    "apps.identification",  # L4 - gemmological findings
    "apps.certificates",  # L5 - certificates and public verification
    "apps.reports",  # L6 - read-only cross-domain reports
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # As early as possible: it must be able to answer a preflight OPTIONS
    # request before CommonMiddleware redirects or otherwise rewrites it.
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # DRF authenticates inside the view, which is too late for the two
    # middlewares below. This resolves the bearer token up front so both of
    # them see the real user instead of AnonymousUser.
    "apps.core.middleware.APIAuthenticationMiddleware",
    # Must follow the two above: it reads request.user.
    "apps.core.current_user.CurrentUserMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "auditlog.middleware.AuditlogMiddleware",
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
            ],
        },
    },
]

DATABASES = {"default": env.db("DATABASE_URL")}
# Every request runs in a transaction: a view that raises leaves no partial write.
DATABASES["default"]["ATOMIC_REQUESTS"] = True

# Browser clients on another origin. Empty by default, so a misconfigured
# deployment blocks requests rather than accepting them from anywhere.
CORS_ALLOWED_ORIGINS = env("CORS_ALLOWED_ORIGINS")
# The JWT travels in the Authorization header, not a cookie, so the browser
# never needs to send credentials cross-origin.
CORS_ALLOW_CREDENTIALS = False

AUTH_USER_MODEL = "users.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 8},
    },
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = env("TIME_ZONE", default="UTC")
# The lab's own calendar. Storage stays in UTC; report filters and seed data use
# this zone, so dates match the lab's local calendar.
LAB_TIME_ZONE = env("LAB_TIME_ZONE", default="Africa/Dar_es_Salaam")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ---------------------------------------------------------------------------
# Django REST Framework
# ---------------------------------------------------------------------------
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        # Simple JWT, plus holding new accounts to their first login.
        "apps.users.authentication.OnboardingJWTAuthentication",
    ],
    # Deny by default. Every view declares its own permissions explicitly, so
    # forgetting to do so fails closed rather than open.
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticated",
    ],
    "DEFAULT_PAGINATION_CLASS": "apps.core.pagination.StandardPagination",
    "PAGE_SIZE": 15,
    "DEFAULT_FILTER_BACKENDS": ["apps.core.filters.WhitelistFilterBackend"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "EXCEPTION_HANDLER": "apps.core.handlers.api_exception_handler",
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.ScopedRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {
        # Tight by design: these are the endpoints worth brute-forcing.
        "auth": "6/min",
    },
    "TEST_REQUEST_DEFAULT_FORMAT": "json",
}

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=env.int("JWT_ACCESS_MINUTES", default=60)),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=env.int("JWT_REFRESH_DAYS", default=14)),
    "ROTATE_REFRESH_TOKENS": True,
    "BLACKLIST_AFTER_ROTATION": True,
    # The auth service stamps last_login_at instead, in one place.
    "UPDATE_LAST_LOGIN": False,
    "AUTH_HEADER_TYPES": ("Bearer",),
}

SPECTACULAR_SETTINGS = {
    "TITLE": "TGPMIS API",
    "DESCRIPTION": (
        "Tanzania Gemmological Centre - stone identification, billing through "
        "the GePG gateway, and certification."
    ),
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
    "SCHEMA_PATH_PREFIX": "/api/v1",
    "COMPONENT_SPLIT_REQUEST": True,
    # Reuse shared choice sets and distinguish each domain's status vocabulary.
    "ENUM_NAME_OVERRIDES": {
        "StoneStatusEnum": "apps.gems.enums.StoneStatus",
        "BillStatusEnum": "apps.gems.enums.BillStatus",
        "CertificateStatusEnum": "apps.gems.enums.CertificateStatus",
        "WeightUnitEnum": "apps.gems.enums.WeightUnit",
    },
}

# Printed on every certificate PDF. Environment-driven so a second lab, or a
# rename, needs no code change.
CERTIFICATE_LAB_NAME = env("CERTIFICATE_LAB_NAME", default="Tanzania Gemmological Centre")
CERTIFICATE_LAB_ADDRESS = env("CERTIFICATE_LAB_ADDRESS", default="")
CERTIFICATE_MINISTRY_NAME = env(
    "CERTIFICATE_MINISTRY_NAME", default="Ministry of Minerals"
)

# Where the QR code printed on a certificate points.
#
# An absolute URL, because a certificate can be rendered with no request in
# hand - a background job, a management command, a test - and a QR that resolves
# only from inside the office is a QR that does not work.
CERTIFICATE_VERIFY_BASE_URL = env(
    "CERTIFICATE_VERIFY_BASE_URL", default="http://localhost:8000"
)

# Where staff sign in; the link in the new-account email points here.
FRONTEND_URL = env("FRONTEND_URL", default="http://localhost:5173")

# Outgoing mail, as one URL: smtp+tls://user:password@host:587 in production.
# The console default prints mail to the server log instead of sending it.
EMAIL_CONFIG = env.email_url("EMAIL_URL", default="consolemail://")
vars().update(EMAIL_CONFIG)
DEFAULT_FROM_EMAIL = env(
    "DEFAULT_FROM_EMAIL", default="Tanzania Gemmological Centre <no-reply@tgc.go.tz>"
)

# Models are registered explicitly in apps.core.audit by walking BaseModel
# subclasses, so auditlog must not blanket-register everything itself.
AUDITLOG_INCLUDE_ALL_MODELS = False

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        # apps.audit parses this exact format back out for the system-log API.
        "standard": {
            "format": "[{asctime}] {levelname} {name}: {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "standard"},
        "file": {
            "class": "logging.handlers.RotatingFileHandler",
            "filename": BASE_DIR / "logs" / "app.log",
            "maxBytes": 10 * 1024 * 1024,
            "backupCount": 5,
            "formatter": "standard",
        },
    },
    "root": {
        "handlers": ["console", "file"],
        "level": env("LOG_LEVEL", default="INFO"),
    },
}

# --- GePG payment gateway ----------------------------------------------------
# The Tanzanian government payment gateway. Bills are submitted for a control
# number, and payment is confirmed asynchronously on a callback - there is no
# manual payment entry anywhere in the system.

GEPG_BILL_CREATE_URL = env("GEPG_BILL_CREATE_URL", default="")
GEPG_BILL_CANCEL_URL = env("GEPG_BILL_CANCEL_URL", default="")
GEPG_RECONCILIATION_URL = env("GEPG_RECONCILIATION_URL", default="")

GEPG_SP_GRP_CODE = env("GEPG_SP_GRP_CODE", default="")
GEPG_SYS_CODE = env("GEPG_SYS_CODE", default="")
GEPG_SP_CODE = env("GEPG_SP_CODE", default="")
GEPG_COM = env("GEPG_COM", default="")
GEPG_ALG = env("GEPG_ALG", default="")
GEPG_SUB_SP_CODE = env("GEPG_SUB_SP_CODE", default="")
GEPG_COLL_CENT_CODE = env("GEPG_COLL_CENT_CODE", default="")
GEPG_GFS_CODE = env("GEPG_GFS_CODE", default="")

GEPG_USE_DIGITAL_SIGNATURE = env.bool("GEPG_USE_DIGITAL_SIGNATURE", default=not DEBUG)
# The legacy integration accepted callbacks without checking their signature.
GEPG_VERIFY_CALLBACK_SIGNATURE = env.bool("GEPG_VERIFY_CALLBACK_SIGNATURE", default=False)
# PKCS#12 password used when loading the outbound signing key.
GEPG_CERTIFICATE_PASSWORD = env("GEPG_CERTIFICATE_PASSWORD", default="")
GEPG_PUBLIC_CERTIFICATE_PASSWORD = env("GEPG_PUBLIC_CERTIFICATE_PASSWORD", default="")
GEPG_PRIVATE_KEY_PATH = env(
    "GEPG_PRIVATE_KEY_PATH", default=str(BASE_DIR / "certificates" / "private.pfx")
)
GEPG_PUBLIC_CERT_PATH = env(
    "GEPG_PUBLIC_CERT_PATH", default=str(BASE_DIR / "certificates" / "public.pfx")
)

GEPG_BILL_EXPIRY_DAYS = env.int("GEPG_BILL_EXPIRY_DAYS", default=365)

# Skips the network call and returns a fake control number, so the whole
# order -> bill -> payment -> certificate flow can be walked offline.
GEPG_SIMULATE = env.bool("GEPG_SIMULATE", default=False)

# False retains the review and manual billing steps.
AUTO_BILL_AFTER_IDENTIFICATION = env.bool("AUTO_BILL_AFTER_IDENTIFICATION", default=False)
GEPG_ACK_SUCCESS_CODES = frozenset({"7101"})

GEPG_RETRY_INITIAL_SECONDS = env.int("GEPG_RETRY_INITIAL_SECONDS", default=60)
GEPG_RETRY_MAX_SECONDS = env.int("GEPG_RETRY_MAX_SECONDS", default=1800)
GEPG_RETRY_MAX_AGE_SECONDS = env.int("GEPG_RETRY_MAX_AGE_SECONDS", default=86400)
GEPG_PENDING_ORDER_COPY_AFTER_SECONDS = env.int(
    "GEPG_PENDING_ORDER_COPY_AFTER_SECONDS", default=600
)

# Control-number messages use the existing Beem account from the retired app.
BEEM_AFRICA_API_KEY = env("BEEM_AFRICA_API_KEY", default="")
BEEM_AFRICA_SECRET_KEY = env("BEEM_AFRICA_SECRET_KEY", default="")
