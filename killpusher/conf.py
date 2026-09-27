from datetime import timedelta

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.utils import timezone

SCOPE = "esi-killmails.read_killmails.v1"
PERMISSION = "killpusher.basic_access"
RETENTION_DAYS = 7


def cutoff():
    return timezone.now() - timedelta(days=RETENTION_DAYS)


def user_agent():
    value = getattr(settings, "KILLPUSHER_USER_AGENT", "")
    if not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value:
        raise ImproperlyConfigured(
            "Set KILLPUSHER_USER_AGENT to include a maintainer URL or email."
        )
    return value
