import os
import secrets
from pathlib import Path

import dj_database_url

BASE = Path(__file__).resolve().parent.parent
LOCAL = BASE / ".local"
HOSTED = bool(os.environ.get("VERCEL"))
if HOSTED:
    SECRET_KEY = os.environ["SECRET_KEY"]
    DATABASES = {
        "default": dj_database_url.parse(
            os.environ["DATABASE_URL"], conn_max_age=0, ssl_require=True
        )
    }
    if DATABASES["default"]["ENGINE"] != "django.db.backends.postgresql":
        raise ValueError("Configure PostgreSQL na Vercel.")
else:
    LOCAL.mkdir(exist_ok=True)
    key_file = LOCAL / "secret"
    if not key_file.exists():
        with key_file.open("x") as key:
            key_file.chmod(0o600)
            key.write(secrets.token_urlsafe(48))
    SECRET_KEY = key_file.read_text().strip()
    db_file = LOCAL / "dburl"
    fallback = (
        db_file.read_text().strip()
        if db_file.exists()
        else "sqlite:///" + str(LOCAL / "te.sqlite3")
    )
    DATABASES = {"default": dj_database_url.config(default=fallback)}
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver"] if not HOSTED else []
ALLOWED_HOSTS += [
    v
    for v in [
        os.getenv("VERCEL_URL"),
        os.getenv("VERCEL_BRANCH_URL"),
        os.getenv("VERCEL_PROJECT_PRODUCTION_URL"),
        *os.getenv("APP_HOSTS", "").split(","),
    ]
    if v
]
ROOT_URLCONF = "estoque.urls"
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "estoque",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE / "templates"],
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ]
        },
    }
]
STATIC_URL = "/static/"
STATIC_ROOT = BASE / ".collected"
STATICFILES_DIRS = [BASE / "static"]
WHITENOISE_USE_FINDERS = not HOSTED
SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = HOSTED
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https") if HOSTED else None
LOGIN_URL = "/entrar/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
LANGUAGE_CODE = "pt-br"
TIME_ZONE = "America/Sao_Paulo"
USE_TZ = True
WSGI_APPLICATION = "estoque.wsgi.application"
INSTALLED_APPS.append("axes")
MIDDLEWARE.append("axes.middleware.AxesMiddleware")
AUTHENTICATION_BACKENDS = [
    "axes.backends.AxesStandaloneBackend",
    "django.contrib.auth.backends.ModelBackend",
]
AXES_FAILURE_LIMIT = 8
AXES_COOLOFF_TIME = 0.25
AXES_LOCKOUT_PARAMETERS = ["username", "ip_address"]
AXES_RESET_ON_SUCCESS = True
AXES_LOCKOUT_TEMPLATE = "locked.html"
if HOSTED:
    SECURE_SSL_REDIRECT = True
    SECURE_HSTS_SECONDS = 31536000
