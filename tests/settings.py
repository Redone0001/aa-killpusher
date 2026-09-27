from allianceauth.project_template.project_name.settings.base import *  # noqa: F403

SECRET_KEY = "tests-only-not-a-production-secret"
SITE_URL = "http://testserver"
CSRF_TRUSTED_ORIGINS = ["http://testserver"]
INSTALLED_APPS = ["modeltranslation", *INSTALLED_APPS, "eve_sde", "killpusher"]  # noqa: F405
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}
CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": "redis://localhost:6379/0",
        "OPTIONS": {"CLIENT_CLASS": "tests.cache.FakeRedisClient"},
    }
}
LOGGING = {"version": 1, "disable_existing_loggers": False}
STATICFILES_DIRS = []
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_BROKER_URL = "memory://"
BROKER_URL = "memory://"
ESI_SSO_CLIENT_ID = "test"
ESI_SSO_CLIENT_SECRET = "test"
ESI_SSO_CALLBACK_URL = "http://testserver/sso/callback"
ESI_USER_CONTACT_EMAIL = "tests@example.invalid"
KILLPUSHER_USER_AGENT = "aa-killpusher tests (tests@example.invalid)"
USE_TZ = True
