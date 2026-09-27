"""Resolve display names in bounded batches, outside user page requests."""

from datetime import timedelta

from allianceauth.eveonline.models import EveCharacter, EveCorporationInfo
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone

from .clients import EsiClient, RemoteError
from .conf import cutoff
from .models import Killmail


def resolve_names():
    if not cache.add("killpusher:names:batch", True, timeout=60):
        return
    now = timezone.now()
    mails = list(
        Killmail.objects.filter(occurred_at__gte=cutoff())
        .filter(
            Q(victim_character_name="", victim_character_id__isnull=False)
            | Q(victim_corporation_name="", victim_corporation_id__isnull=False)
        )
        .filter(Q(next_name_check__isnull=True) | Q(next_name_check__lte=now))
        .order_by("next_name_check", "id")[:100]
    )
    if not mails:
        return
    # Failed/unresolvable batches move behind untouched records rather than starving them.
    Killmail.objects.filter(pk__in=[mail.pk for mail in mails]).update(
        next_name_check=now + timedelta(minutes=5),
    )
    ids = {
        value
        for mail in mails
        for value in (
            mail.victim_character_id,
            mail.victim_corporation_id,
        )
        if value
    }
    resolved = dict(
        EveCharacter.objects.filter(character_id__in=ids).values_list(
            "character_id",
            "character_name",
        )
    )
    resolved.update(
        EveCorporationInfo.objects.filter(corporation_id__in=ids).values_list(
            "corporation_id",
            "corporation_name",
        )
    )
    resolved = {entity_id: name for entity_id, name in resolved.items() if name}
    keys = {entity_id: f"killpusher:name:{entity_id}" for entity_id in ids}
    cached = cache.get_many(keys.values())
    resolved.update({entity_id: cached[key] for entity_id, key in keys.items() if cached.get(key)})
    missing = {entity_id for entity_id in ids - resolved.keys() if keys[entity_id] not in cached}
    if missing:
        try:
            rows = EsiClient().names(missing)
        except RemoteError:
            rows = []
        valid = {
            row["id"]: row["name"]
            for row in rows
            if isinstance(row, dict)
            and row.get("id") in missing
            and isinstance(row.get("name"), str)
            and row.get("category") in ("character", "corporation")
            and 0 < len(row["name"]) <= 255
        }
        cache.set_many({keys[entity_id]: name for entity_id, name in valid.items()}, timeout=604800)
        cache.set_many({keys[entity_id]: "" for entity_id in missing - valid.keys()}, timeout=300)
        resolved.update(valid)
    for mail in mails:
        changes = {}
        for kind in ("character", "corporation"):
            name = resolved.get(getattr(mail, f"victim_{kind}_id"))
            if name:
                changes[f"victim_{kind}_name"] = name
        if changes:
            Killmail.objects.filter(pk=mail.pk).update(**changes)
