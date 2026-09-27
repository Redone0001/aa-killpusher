import logging
import re
import time
import uuid
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from esi.errors import TokenError

from .access import valid_token
from .clients import EsiClient, RemoteError
from .conf import cutoff
from .models import CharacterKillmail, Killmail, TrackedCharacter

logger = logging.getLogger(__name__)
HASH = re.compile(r"[0-9a-fA-F]{40}\Z")
PAGE_BUDGET = 5


def reference(row):
    mail_id = row["killmail_id"]
    mail_hash = row["killmail_hash"]
    if not isinstance(mail_id, int) or mail_id <= 0 or not HASH.fullmatch(mail_hash):
        raise ValueError("Invalid killmail reference")
    return mail_id, mail_hash


def store_detail(character, mail_id, mail_hash, payload):
    occurred = parse_datetime(payload["killmail_time"])
    if payload["killmail_id"] != mail_id or occurred is None or timezone.is_naive(occurred):
        raise ValueError("Invalid killmail detail")
    if occurred < cutoff():
        return False
    victim = payload["victim"]
    own_id = character.ownership.character.character_id
    is_loss = victim.get("character_id") == own_id
    if not is_loss and not any(a.get("character_id") == own_id for a in payload["attackers"]):
        raise ValueError("Character not involved in killmail")
    with transaction.atomic():
        # Recheck ownership/token under lock so disconnecting during an HTTP call cannot
        # leave details visible to a subsequent owner or recreate disconnected links.
        current = TrackedCharacter.objects.select_for_update().filter(pk=character.pk).first()
        if current is None or current.token_id != character.token_id or not valid_token(current):
            raise RemoteError("Character disconnected or ownership changed.", unauthorized=True)
        killmail, _ = Killmail.objects.get_or_create(
            id=mail_id,
            defaults={
                "hash": mail_hash,
                "occurred_at": occurred,
                "solar_system_id": payload["solar_system_id"],
                "victim_character_id": victim.get("character_id"),
                "victim_corporation_id": victim.get("corporation_id"),
                "victim_alliance_id": victim.get("alliance_id"),
                "victim_ship_type_id": victim["ship_type_id"],
            },
        )
        CharacterKillmail.objects.get_or_create(
            character=current,
            killmail=killmail,
            defaults={"role": "loss" if is_loss else "kill"},
        )
    return True


def sync_character(character_pk):
    now = timezone.now()
    lease = uuid.uuid4()
    available = (
        TrackedCharacter.objects.filter(pk=character_pk)
        .filter(
            Q(lock_until__isnull=True) | Q(lock_until__lt=now),
        )
        .filter(Q(next_poll__isnull=True) | Q(next_poll__lte=now))
    )
    if not available.update(
        sync_lock=lease,
        lock_until=now + timedelta(minutes=5),
        next_poll=now + timedelta(seconds=300),
    ):
        return
    owned = TrackedCharacter.objects.filter(pk=character_pk, sync_lock=lease)
    character = owned.select_related("ownership__character", "ownership__user", "token").first()
    if character is None:
        return  # Disconnected or reauthorized after the lease was claimed.
    started = time.monotonic()
    updates = {"next_poll": now + timedelta(seconds=300), "error": ""}
    try:
        if not valid_token(character):
            raise RemoteError(
                "Reconnect this character to restore ESI authorization.", unauthorized=True
            )
        client = EsiClient(character.token)
        cursor = character.next_page
        # Always inspect newest kills; overlap a backfill page to accommodate moving pages.
        pages = [1]
        continuation = max(2, cursor - 1) if cursor > 1 else 2
        pages.extend(range(continuation, continuation + PAGE_BUDGET - 1))
        for page in pages:
            if time.monotonic() - started > 180:
                break
            rows, total_pages = client.recent(character.ownership.character.character_id, page)
            all_old = bool(rows)
            for row in rows:
                if time.monotonic() - started > 210:
                    raise RemoteError("Import continues on the next poll.")
                mail_id, mail_hash = reference(row)
                existing = Killmail.objects.filter(pk=mail_id).first()
                if (
                    existing is not None
                    and CharacterKillmail.objects.filter(
                        character=character,
                        killmail=existing,
                    ).exists()
                ):
                    all_old = all_old and existing.occurred_at < cutoff()
                    continue
                payload = client.detail(mail_id, mail_hash)
                kept = store_detail(character, mail_id, mail_hash, payload)
                all_old = all_old and not kept
            # Pages are most recent first. Stop once a full page is outside retention.
            if not rows or all_old or page >= total_pages:
                updates["next_page"] = 1
                break
            if page != 1 or cursor <= 2:
                updates["next_page"] = page + 1
        updates["last_sync"] = timezone.now()
    except (RemoteError, TokenError) as exc:
        if isinstance(exc, TokenError) or getattr(exc, "unauthorized", False):
            updates["token"] = None
            updates["error"] = "Reconnect this character to restore ESI authorization."
        else:
            updates["error"] = str(exc)
            updates["next_poll"] = timezone.now() + timedelta(seconds=exc.retry_after)
    except (KeyError, ValueError, TypeError):
        updates["error"] = "Unexpected CCP data; import will retry."
        logger.warning("Unexpected CCP data for tracked character %s", character_pk)
    finally:
        owned.update(**updates, sync_lock=None, lock_until=None)
