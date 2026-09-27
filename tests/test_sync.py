from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.utils import timezone
from esi.errors import TokenInvalidError

from killpusher import posting
from killpusher.clients import EsiClient, RemoteError
from killpusher.models import CharacterKillmail, Killmail, Submission, TrackedCharacter
from killpusher.sync import store_detail, sync_character
from killpusher.tasks import cleanup, poll_killmails

pytestmark = pytest.mark.django_db


def register_esi(requests_mock, tracked, payload, page=1, pages=1):
    char_id = tracked.ownership.character.character_id
    endpoint = requests_mock.get(
        f"https://esi.evetech.net/characters/{char_id}/killmails/recent?page={page}",
        json=[{"killmail_id": payload["killmail_id"], "killmail_hash": "a" * 40}],
        headers={"X-Pages": str(pages)},
    )
    requests_mock.get(
        f"https://esi.evetech.net/killmails/{payload['killmail_id']}/{'a' * 40}",
        json=payload,
    )
    return endpoint


def test_import_minimal_and_skip_repeat_details(pilot, payload, requests_mock):
    _, tracked = pilot
    endpoint = register_esi(requests_mock, tracked, payload)
    sync_character(tracked.pk)
    mail = Killmail.objects.get()
    assert mail.victim_ship_type_id == 587
    assert CharacterKillmail.objects.get().role == "kill"
    assert not {"items", "attackers", "position", "payload"}.intersection(mail.__dict__)
    assert endpoint.last_request.headers["Authorization"] == "Bearer test-access-token"
    assert endpoint.last_request.headers["X-Compatibility-Date"] == "2026-09-27"
    TrackedCharacter.objects.filter(pk=tracked.pk).update(next_poll=None)
    requests_mock.reset_mock()
    sync_character(tracked.pk)
    assert requests_mock.call_count == 1  # Recent list only; detail is already local.
    assert Killmail.objects.count() == 1


def test_losses_and_optional_npc_fields(pilot, payload):
    _, tracked = pilot
    payload["victim"]["character_id"] = tracked.ownership.character.character_id
    del payload["victim"]["alliance_id"]
    store_detail(tracked, payload["killmail_id"], "a" * 40, payload)
    assert CharacterKillmail.objects.get().role == "loss"
    assert Killmail.objects.get().victim_alliance_id is None


def test_unrelated_character_payload_not_saved(pilot, payload):
    _, tracked = pilot
    payload["attackers"] = []
    with pytest.raises(ValueError):
        store_detail(tracked, payload["killmail_id"], "a" * 40, payload)
    assert not Killmail.objects.exists()


def test_expired_payload_not_reimported(pilot, payload, requests_mock):
    _, tracked = pilot
    payload["killmail_time"] = (timezone.now() - timedelta(days=8)).isoformat()
    register_esi(requests_mock, tracked, payload, pages=8)
    sync_character(tracked.pk)
    assert not Killmail.objects.exists()
    tracked.refresh_from_db()
    assert tracked.next_page == 1
    assert tracked.last_sync


def test_pagination_and_partial_failure_resume(pilot, payload, requests_mock):
    _, tracked = pilot
    register_esi(requests_mock, tracked, payload, pages=2)
    second = deepcopy(payload)
    second["killmail_id"] += 1
    register_esi(requests_mock, tracked, second, page=2, pages=2)
    detail = f"https://esi.evetech.net/killmails/{second['killmail_id']}/{'a' * 40}"
    requests_mock.get(detail, status_code=503)
    sync_character(tracked.pk)
    tracked.refresh_from_db()
    assert tracked.next_page == 2
    assert tracked.error
    assert Killmail.objects.count() == 1
    requests_mock.get(detail, json=second)
    TrackedCharacter.objects.filter(pk=tracked.pk).update(next_poll=None)
    sync_character(tracked.pk)
    tracked.refresh_from_db()
    assert not tracked.error
    assert tracked.next_page == 1
    assert Killmail.objects.count() == 2


def test_page_budget_defers_large_backfill(pilot, payload, requests_mock):
    _, tracked = pilot
    for page in range(1, 11):
        data = deepcopy(payload)
        data["killmail_id"] += page
        register_esi(requests_mock, tracked, data, page=page, pages=10)
    sync_character(tracked.pk)
    tracked.refresh_from_db()
    assert tracked.next_page == 6
    assert Killmail.objects.count() == 5
    TrackedCharacter.objects.filter(pk=tracked.pk).update(next_poll=None)
    sync_character(tracked.pk)
    tracked.refresh_from_db()
    assert tracked.next_page == 9
    assert Killmail.objects.count() == 8


def test_locked_and_not_due_characters_do_not_fetch(pilot):
    _, tracked = pilot
    with patch("killpusher.sync.EsiClient") as client:
        TrackedCharacter.objects.filter(pk=tracked.pk).update(
            lock_until=timezone.now() + timedelta(minutes=5)
        )
        sync_character(tracked.pk)
        TrackedCharacter.objects.filter(pk=tracked.pk).update(
            lock_until=None, next_poll=timezone.now() + timedelta(minutes=5)
        )
        sync_character(tracked.pk)
        client.assert_not_called()


def test_revoked_token_releases_lock_and_requires_reconnect(pilot):
    _, tracked = pilot
    with patch("esi.models.Token.valid_access_token", side_effect=TokenInvalidError):
        sync_character(tracked.pk)
    tracked.refresh_from_db()
    assert tracked.token is None
    assert tracked.lock_until is None
    assert "Reconnect" in tracked.error


def test_rate_limit_schedules_later_without_erasing_token(pilot, requests_mock):
    _, tracked = pilot
    url = f"https://esi.evetech.net/characters/{tracked.ownership.character.character_id}/killmails/recent"
    requests_mock.get(url, status_code=429, headers={"Retry-After": "1200"})
    sync_character(tracked.pk)
    tracked.refresh_from_db()
    assert tracked.next_poll > timezone.now() + timedelta(minutes=19)
    assert tracked.token is not None
    assert "rate limit" in tracked.error


def test_global_esi_error_budget_backoff(pilot, requests_mock):
    _, tracked = pilot
    requests_mock.get(
        "https://esi.evetech.net/test", status_code=420, headers={"X-Esi-Error-Limit-Reset": "60"}
    )
    with pytest.raises(RemoteError):
        EsiClient(tracked.token).get("/test")
    assert cache.get("killpusher:esi:backoff")
    with pytest.raises(RemoteError):
        EsiClient(tracked.token).get("/other")
    assert requests_mock.call_count == 1


def test_ownership_change_during_request_does_not_save(pilot, payload, make_character):
    _, tracked = pilot
    other, _ = make_character(2)
    ownership = tracked.ownership
    ownership.user = other
    ownership.save()
    with pytest.raises(RemoteError):
        store_detail(tracked, payload["killmail_id"], "a" * 40, payload)
    assert not Killmail.objects.exists()


def test_disconnect_during_request_does_not_restore_data(pilot, payload):
    _, tracked = pilot
    TrackedCharacter.objects.filter(pk=tracked.pk).delete()
    with pytest.raises(RemoteError):
        store_detail(tracked, payload["killmail_id"], "a" * 40, payload)
    assert not Killmail.objects.exists()


def test_cleanup_keeps_durable_deduplication(pilot, make_mail):
    user, tracked = pilot
    mail = make_mail(tracked, occurred=timezone.now() - timedelta(days=8))
    submission, _ = posting.claim(mail.pk, user)
    submission.state = "submitted"
    submission.save()
    cleanup()
    assert not Killmail.objects.exists()
    assert not CharacterKillmail.objects.exists()
    assert Submission.objects.get(pk=mail.pk).state == "submitted"
    assert posting.claim(mail.pk, user)[1] is False


def test_scheduled_task_enqueues_authorized_characters(pilot):
    _, tracked = pilot
    with patch("killpusher.tasks.fetch_character.apply_async") as enqueue:
        poll_killmails()
        enqueue.assert_called_once_with(args=[tracked.pk], expires=300)


def test_reconnect_during_fetch_cannot_erase_new_token(client, pilot, payload):
    from django.urls import reverse
    from esi.models import Token

    _, tracked = pilot
    client.force_login(tracked.user)
    old_token = tracked.token
    new_token = Token.objects.create(
        user=tracked.user,
        character_id=old_token.character_id,
        character_name=old_token.character_name,
        character_owner_hash=old_token.character_owner_hash,
        access_token="replacement-access-token",
        refresh_token="replacement-refresh-token",
    )
    new_token.scopes.set(old_token.scopes.all())

    def reconnect_then_fail(*args):
        with patch("esi.decorators._check_callback", return_value=new_token):
            assert client.get(reverse("killpusher:connect")).status_code == 302
        raise RemoteError("Old token revoked", unauthorized=True)

    with patch("killpusher.sync.EsiClient.recent", side_effect=reconnect_then_fail):
        sync_character(tracked.pk)
    tracked.refresh_from_db()
    assert tracked.token_id == new_token.pk
    assert not tracked.error


@pytest.mark.parametrize("change", ["scope", "owner_hash", "permission", "inactive"])
def test_invalid_authorization_never_calls_ccp(pilot, change):
    user, tracked = pilot
    if change == "scope":
        tracked.token.scopes.clear()
    elif change == "owner_hash":
        tracked.token.character_owner_hash = "different-owner"
        tracked.token.save()
    elif change == "permission":
        user.user_permissions.clear()
    elif change == "inactive":
        user.is_active = False
        user.save()
    with patch("killpusher.sync.EsiClient") as http:
        sync_character(tracked.pk)
        http.assert_not_called()
    tracked.refresh_from_db()
    assert tracked.token_id is None
