from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from kombu.exceptions import OperationalError

from killpusher.models import TrackedCharacter
from killpusher.queueing import queue_import

pytestmark = pytest.mark.django_db


def test_new_connection_queues_only_after_commit(client, pilot, django_capture_on_commit_callbacks):
    user, tracked = pilot
    token = tracked.token
    tracked.delete()
    client.force_login(user)
    with (
        patch("esi.decorators._check_callback", return_value=token),
        patch("killpusher.views.queue_import") as queue,
    ):
        with django_capture_on_commit_callbacks(execute=False) as callbacks:
            response = client.get(reverse("killpusher:connect"))
        assert response.status_code == 302
        queue.assert_not_called()
        assert len(callbacks) == 1
        callbacks[0]()
        queue.assert_called_once_with(TrackedCharacter.objects.get().pk)


def test_reconnect_also_queues_import(client, pilot, django_capture_on_commit_callbacks):
    user, tracked = pilot
    client.force_login(user)
    with (
        patch("esi.decorators._check_callback", return_value=tracked.token),
        patch("killpusher.views.queue_import") as queue,
    ):
        with django_capture_on_commit_callbacks(execute=True):
            client.get(reverse("killpusher:connect"))
        queue.assert_called_once_with(tracked.pk)


def test_queue_immediately_for_new_character(pilot):
    _, tracked = pilot
    with patch("killpusher.queueing.fetch_character.apply_async") as queue:
        assert queue_import(tracked.pk)
    assert queue.call_args.kwargs["args"] == [tracked.pk]
    assert "eta" not in queue.call_args.kwargs


def test_queue_respects_rate_limit_and_lease(pilot):
    _, tracked = pilot
    later = timezone.now() + timedelta(minutes=20)
    tracked.next_poll = later
    tracked.lock_until = later + timedelta(minutes=1)
    tracked.save()
    with patch("killpusher.queueing.fetch_character.apply_async") as queue:
        assert queue_import(tracked.pk)
    assert queue.call_args.kwargs["eta"] == tracked.lock_until
    assert queue.call_args.kwargs["expires"] == tracked.lock_until + timedelta(minutes=5)


def test_missing_or_disconnected_character_is_not_queued(pilot):
    _, tracked = pilot
    tracked.token = None
    tracked.save()
    with patch("killpusher.queueing.fetch_character.apply_async") as queue:
        assert not queue_import(tracked.pk)
        assert not queue_import(tracked.pk + 999)
        queue.assert_not_called()


def test_broker_failure_keeps_authorization(pilot):
    _, tracked = pilot
    token_pk = tracked.token_id
    with patch(
        "killpusher.queueing.fetch_character.apply_async",
        side_effect=OperationalError("broker down"),
    ):
        assert not queue_import(tracked.pk)
    tracked.refresh_from_db()
    assert tracked.token_id == token_pk
    assert "scheduled poll will retry" in tracked.error


def test_refresh_all_includes_own_alts_only(client, pilot, make_character):
    user, main = pilot
    _, alt = make_character(2)
    _, outsider = make_character(3)
    alt.user = user
    alt.save()
    alt.ownership.user = user
    alt.ownership.save()
    alt.token.user = user
    alt.token.save()
    client.force_login(user)
    with patch("killpusher.views.queue_import", return_value=True) as queue:
        response = client.post(reverse("killpusher:refresh"))
    assert response.status_code == 202
    assert response.json()["queued"] == 2
    assert {call.args[0] for call in queue.call_args_list} == {main.pk, alt.pk}
    assert outsider.pk not in {call.args[0] for call in queue.call_args_list}


def test_refresh_throttles_repeated_clicks(client, pilot):
    user, _ = pilot
    client.force_login(user)
    with patch("killpusher.views.queue_import", return_value=True) as queue:
        assert client.post(reverse("killpusher:refresh")).status_code == 202
        assert client.post(reverse("killpusher:refresh")).status_code == 429
        queue.assert_called_once()


def test_refresh_skips_invalid_tokens(client, pilot):
    user, tracked = pilot
    tracked.token.scopes.clear()
    client.force_login(user)
    with patch("killpusher.views.queue_import") as queue:
        response = client.post(reverse("killpusher:refresh"))
        assert response.status_code == 400
        queue.assert_not_called()


def test_refresh_reports_queue_failure_and_allows_retry(client, pilot):
    user, _ = pilot
    client.force_login(user)
    with patch("killpusher.views.queue_import", return_value=False):
        response = client.post(reverse("killpusher:refresh"))
    assert response.status_code == 503
    assert response.json()["failed"] == 1
    assert cache.get(f"killpusher:refresh:{user.pk}") is None


def test_refresh_requires_login_permission_post_and_csrf(client, pilot):
    user, _ = pilot
    url = reverse("killpusher:refresh")
    assert client.post(url).status_code == 302
    client.force_login(user)
    assert client.get(url).status_code == 405
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(user)
    assert csrf_client.post(url).status_code == 403
    user.user_permissions.clear()
    assert client.post(url).status_code == 403


def test_list_exposes_refresh_control(client, pilot):
    user, _ = pilot
    client.force_login(user)
    body = client.get(reverse("killpusher:index")).content.decode()
    assert "Refresh all characters" in body
    assert reverse("killpusher:refresh") in body
