"""Local synthetic browser fixture. Never contacts ESI or zKillboard."""

import os
import tempfile
from datetime import timedelta

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "tests.settings")

import django  # noqa: E402
from django.conf import settings  # noqa: E402

preview_db = tempfile.NamedTemporaryFile(prefix="killpusher-preview-", suffix=".sqlite3")
settings.DATABASES["default"]["NAME"] = preview_db.name
settings.ROOT_URLCONF = "tests.preview_urls"
settings.WSGI_APPLICATION = None
settings.SITE_NAME = "Killmail Pusher · Demo data"
settings.SESSION_COOKIE_SECURE = False
settings.CSRF_COOKIE_SECURE = False
django.setup()

from allianceauth.tests.auth_utils import AuthUtils  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.utils import timezone  # noqa: E402
from eve_sde.models import ItemType, SolarSystem  # noqa: E402

from killpusher import clients, views  # noqa: E402


def main():
    from tests.conftest import make_character, make_mail

    call_command("migrate", verbosity=0)
    AuthUtils.disconnect_signals()
    user, tracked = make_character.__wrapped__(None)()
    tracked.last_sync = timezone.now()
    tracked.save()
    create_mail = make_mail.__wrapped__(None)
    create_mail(tracked)
    from killpusher.models import Killmail
    from killpusher.posting import mark_public

    Killmail.objects.update(
        victim_character_name="Example Pilot", victim_corporation_name="Example Corporation"
    )
    mark_public(123456789)
    create_mail(
        tracked,
        mail_id=123456788,
        alliance=99000001,
        role="loss",
        occurred=timezone.now() - timedelta(hours=2),
    )
    ItemType.objects.create(id=587, name="Rifter")
    SolarSystem.objects.create(id=30000142, name="Jita")
    clients.post_killmail = lambda mail: clients.PostResult(
        "submitted", "Accepted by demo service."
    )
    clients.is_public = lambda mail_id: True
    views.queue_import = lambda character_pk: True
    call_command("runserver", "127.0.0.1:8877", use_reloader=False)


if __name__ == "__main__":
    main()
