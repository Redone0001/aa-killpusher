import pytest
from allianceauth.authentication.models import CharacterOwnership
from allianceauth.tests.auth_utils import AuthUtils
from django.contrib.auth.models import Permission
from django.core.cache import cache
from django.utils import timezone
from esi.models import Scope, Token

from killpusher.conf import SCOPE
from killpusher.models import CharacterKillmail, Killmail, TrackedCharacter


@pytest.fixture(autouse=True)
def isolated_aa_signals():
    AuthUtils.disconnect_signals()
    cache.clear()
    yield
    AuthUtils.connect_signals()


@pytest.fixture
def make_character(db):
    def make(number=1, alliance=99000001):
        user = AuthUtils.create_user(f"pilot{number}")
        user.user_permissions.add(Permission.objects.get(codename="basic_access"))
        main = AuthUtils.add_main_character_2(
            user,
            f"Pilot {number}",
            90000000 + number,
            alliance_id=alliance,
            alliance_name="Our alliance" if alliance else "",
        )
        ownership = CharacterOwnership.objects.create(
            user=user,
            character=main,
            owner_hash=f"owner-{number}",
        )
        token = Token.objects.create(
            user=user,
            character_id=main.character_id,
            character_name=main.character_name,
            character_owner_hash=ownership.owner_hash,
            access_token="test-access-token",
            refresh_token="test-refresh-token",
        )
        token.scopes.add(Scope.objects.get_or_create(name=SCOPE)[0])
        tracked = TrackedCharacter.objects.create(
            user=user,
            owner_hash=ownership.owner_hash,
            ownership=ownership,
            token=token,
        )
        return user, tracked

    return make


@pytest.fixture
def pilot(make_character):
    return make_character()


@pytest.fixture
def make_mail(db):
    def make(tracked, mail_id=123456789, alliance=99000002, role="kill", occurred=None):
        mail, _ = Killmail.objects.get_or_create(
            id=mail_id,
            defaults={
                "hash": "a" * 40,
                "occurred_at": occurred or timezone.now(),
                "solar_system_id": 30000142,
                "victim_character_id": 90000100,
                "victim_corporation_id": 98000001,
                "victim_alliance_id": alliance,
                "victim_ship_type_id": 587,
            },
        )
        CharacterKillmail.objects.get_or_create(
            character=tracked,
            killmail=mail,
            defaults={"role": role},
        )
        return mail

    return make


@pytest.fixture
def payload(pilot):
    _, tracked = pilot
    return {
        "killmail_id": 123456789,
        "killmail_time": timezone.now().isoformat(),
        "solar_system_id": 30000142,
        "victim": {
            "character_id": 90000100,
            "corporation_id": 98000001,
            "alliance_id": 99000002,
            "ship_type_id": 587,
            "damage_taken": 500,
            "items": [{"item_type_id": 34, "quantity_destroyed": 100}],
            "position": {"x": 1, "y": 2, "z": 3},
        },
        "attackers": [
            {
                "character_id": tracked.ownership.character.character_id,
                "alliance_id": 99000001,
                "final_blow": True,
                "damage_done": 500,
                "security_status": 0.0,
            }
        ],
    }
