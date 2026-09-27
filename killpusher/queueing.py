"""User-triggered imports share the scheduled import's cache and locking rules."""

import logging
from datetime import timedelta

from django.utils import timezone
from kombu.exceptions import OperationalError

from .models import TrackedCharacter
from .tasks import fetch_character

logger = logging.getLogger(__name__)


def queue_import(character_pk):
    character = TrackedCharacter.objects.filter(pk=character_pk, token__isnull=False).first()
    if character is None:
        return False
    now = timezone.now()
    earliest = max(value for value in (now, character.next_poll, character.lock_until) if value)
    options = {"args": [character_pk], "expires": earliest + timedelta(minutes=5)}
    if earliest > now:
        options["eta"] = earliest
    try:
        fetch_character.apply_async(**options)
    except OperationalError:
        # Authorization is already committed. Keep it intact and let Beat retry.
        logger.warning(
            "Could not queue import for character %s; scheduled poll will retry", character_pk
        )
        TrackedCharacter.objects.filter(pk=character_pk, token_id=character.token_id).update(
            error="Could not queue import. The scheduled poll will retry.",
        )
        return False
    return True
