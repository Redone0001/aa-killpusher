from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.test import Client
from django.urls import reverse

from killpusher.models import TrackedCharacter

pytestmark = pytest.mark.django_db


def test_import_reuses_token_after_commit(client, pilot, django_capture_on_commit_callbacks):
    user, tracked = pilot
    token = tracked.token
    tracked.delete()
    client.force_login(user)
    with patch("killpusher.views.queue_import") as queue:
        with django_capture_on_commit_callbacks(execute=False) as callbacks:
            response = client.post(reverse("killpusher:import_characters"))
        assert response.status_code == 202
        assert response.json()["connected"] == 1
        queue.assert_not_called()
        callbacks[0]()
        imported = TrackedCharacter.objects.get()
        assert imported.token_id == token.pk
        queue.assert_called_once_with(imported.pk)


@pytest.mark.parametrize("invalid", ["scope", "user", "hash"])
def test_import_rejects_ineligible_tokens(client, pilot, invalid):
    user, tracked = pilot
    token = tracked.token
    tracked.delete()
    if invalid == "scope":
        token.scopes.clear()
    elif invalid == "user":
        token.user = None
        token.save()
    else:
        token.character_owner_hash = "wrong-owner"
        token.save()
    client.force_login(user)
    response = client.post(reverse("killpusher:import_characters"))
    assert response.json()["missing"] == 1
    assert not TrackedCharacter.objects.exists()


def test_existing_connections_and_other_accounts_untouched(client, pilot, make_character):
    user, tracked = pilot
    _, other = make_character(2)
    other.delete()
    before = TrackedCharacter.objects.values().get()
    client.force_login(user)
    response = client.post(reverse("killpusher:import_characters"))
    assert response.json()["skipped"] == 1
    assert response.json()["connected"] == 0
    assert TrackedCharacter.objects.values().get() == before
    cache.clear()
    assert client.post(reverse("killpusher:import_characters")).json()["skipped"] == 1


def test_import_security_and_throttle(client, pilot):
    user, _ = pilot
    url = reverse("killpusher:import_characters")
    assert client.post(url).status_code == 302
    client.force_login(user)
    assert client.get(url).status_code == 405
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(user)
    assert csrf_client.post(url).status_code == 403
    assert client.post(url).status_code == 202
    assert client.post(url).status_code == 429
    user.user_permissions.clear()
    assert client.post(url).status_code == 403
