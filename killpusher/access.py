from django.core.exceptions import ObjectDoesNotExist
from django.db.models import F

from .conf import PERMISSION, SCOPE
from .models import TrackedCharacter


def tracked_for(user):
    return TrackedCharacter.objects.filter(
        user=user,
        ownership__user=user,
        owner_hash=F("ownership__owner_hash"),
    ).select_related(
        "ownership__character",
        "ownership__user",
        "token",
    )


def current_alliance(user):
    try:
        main = user.profile.main_character
    except ObjectDoesNotExist:
        return None
    return main.alliance_id if main else None


def valid_token(character):
    token = character.token
    ownership = character.ownership
    return bool(
        token
        and character.user_id == ownership.user_id
        and character.owner_hash == ownership.owner_hash
        and ownership.user.is_active
        and ownership.user.has_perm(PERMISSION)
        and token.user_id == ownership.user_id
        and token.character_id == ownership.character.character_id
        and token.character_owner_hash == ownership.owner_hash
        and token.scopes.filter(name=SCOPE).exists()
    )
