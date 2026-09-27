from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from . import __version__

SCOPE = "esi-killmails.read_killmails.v1"
PERMISSION = "killpusher.basic_access"
RETENTION_DAYS = 7


def cutoff():
    return timezone.now() - timedelta(days=RETENTION_DAYS)


def user_agent():
    """Identify requests using AA's centrally configured site and maintainer."""
    # Preserve existing explicit overrides, but no module setting is required.
    override = _header_text(getattr(settings, "KILLPUSHER_USER_AGENT", ""))
    if override:
        return override
    contact = [
        _header_text(getattr(settings, name, "")) for name in ("SITE_URL", "ESI_USER_CONTACT_EMAIL")
    ]
    details = "; ".join(value for value in contact if value)
    if not details:
        details = "https://github.com/Redone0001/aa-killpusher"
    return f"aa-killpusher/{__version__} ({details})"


def _header_text(value):
    if not isinstance(value, str):
        return ""
    return " ".join(value.split()).encode("ascii", errors="replace").decode("ascii")
