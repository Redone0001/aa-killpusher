from datetime import timedelta
from unittest.mock import patch

import pytest
import requests
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from killpusher import clients, posting
from killpusher.models import Submission

pytestmark = pytest.mark.django_db


def post_url(mail):
    return f"https://zkillboard.com/api/killmail/add/{mail.pk}/{mail.hash}/"


@pytest.mark.parametrize("new", [True, False])
def test_success_prevents_global_repost(pilot, make_character, make_mail, requests_mock, new):
    user, tracked = pilot
    other, other_tracked = make_character(2)
    mail = make_mail(tracked)
    make_mail(other_tracked)
    endpoint = requests_mock.post(post_url(mail), json={"status": "success", "new": new})
    assert posting.submit(mail, user).state == "submitted"
    assert posting.submit(mail, other).state == "submitted"
    assert endpoint.call_count == 1
    assert Submission.objects.get(pk=mail.pk).submitted_by == user
    assert "test-access-token" not in str(endpoint.last_request.headers)


def test_inflight_request_cannot_be_claimed_twice(pilot, make_mail):
    user, tracked = pilot
    mail = make_mail(tracked)

    def upstream(_mail):
        duplicate, claimed = posting.claim(mail.pk, user)
        assert not claimed
        assert duplicate.state == "sending"
        return clients.PostResult("submitted", "Accepted")

    with patch("killpusher.clients.post_killmail", side_effect=upstream) as mocked:
        assert posting.submit(mail, user).state == "submitted"
        assert mocked.call_count == 1


@pytest.mark.parametrize(
    "status,body,expected",
    [
        (408, {"error": "timeout"}, "unknown"),
        (500, {"error": "upstream"}, "unknown"),
        (200, {"status": "error"}, "unknown"),
        (200, [], "unknown"),
        (302, {}, "unknown"),
        (401, {"error": "you cannot be trusted"}, "rejected"),
        (422, {"status": "error", "error": "invalid"}, "rejected"),
    ],
)
def test_upstream_outcomes(pilot, make_mail, requests_mock, status, body, expected):
    user, tracked = pilot
    mail = make_mail(tracked)
    requests_mock.post(post_url(mail), json=body, status_code=status)
    assert posting.submit(mail, user).state == expected


def test_timeout_is_locked_and_cannot_be_retried(pilot, make_mail, requests_mock):
    user, tracked = pilot
    mail = make_mail(tracked)
    endpoint = requests_mock.post(post_url(mail), exc=requests.Timeout)
    assert posting.submit(mail, user).state == "unknown"
    assert posting.submit(mail, user).state == "unknown"
    assert endpoint.call_count == 1


def test_rejected_request_can_be_retried(pilot, make_mail, requests_mock):
    user, tracked = pilot
    mail = make_mail(tracked)
    endpoint = requests_mock.post(
        post_url(mail),
        [
            {"status_code": 422, "json": {"error": "ESI unavailable"}},
            {"status_code": 200, "json": {"status": "success"}},
        ],
    )
    assert posting.submit(mail, user).state == "rejected"
    cache.delete("killpusher:zkill:request")  # Simulate the shared request cooldown expiring.
    assert posting.submit(mail, user).state == "submitted"
    assert endpoint.call_count == 2


def test_friendly_requires_signed_confirmation(client, pilot, make_mail, requests_mock):
    user, tracked = pilot
    mail = make_mail(tracked, alliance=99000001)
    endpoint = requests_mock.post(post_url(mail), json={"status": "success"})
    client.force_login(user)
    url = reverse("killpusher:push", args=[mail.pk])
    response = client.post(url, {"confirmed": "true"})
    assert response.status_code == 409
    assert not Submission.objects.exists()
    assert endpoint.call_count == 0
    assert client.post(url, {"confirmation": "forged"}).status_code == 409
    result = client.post(url, {"confirmation": response.json()["confirmation"]})
    assert result.json()["state"] == "submitted"
    assert endpoint.call_count == 1


def test_confirmation_bound_to_user_mail_and_current_alliance(pilot, make_character, make_mail):
    user, tracked = pilot
    other, _ = make_character(2)
    mail = make_mail(tracked, alliance=99000001)
    second = make_mail(tracked, mail_id=987654321, alliance=99000001)
    signed = posting.confirmation_needed(user, mail, None)
    assert posting.confirmation_needed(user, mail, signed) is None
    assert posting.confirmation_needed(other, mail, signed)
    assert posting.confirmation_needed(user, second, signed)
    user.profile.main_character.alliance_id = 99000003
    mail.victim_alliance_id = 99000003
    assert posting.confirmation_needed(user, mail, signed)


def test_expired_confirmation_requires_new_prompt(pilot, make_mail):
    user, tracked = pilot
    mail = make_mail(tracked, alliance=99000001)
    with patch("django.core.signing.time.time", return_value=1000):
        signed = posting.confirmation_needed(user, mail, None)
    assert posting.confirmation_needed(user, mail, signed)


def test_no_alliance_is_not_friendly(make_character, make_mail):
    user, tracked = make_character(alliance=None)
    mail = make_mail(tracked, alliance=None)
    assert posting.confirmation_needed(user, mail, None) is None


def test_reconciliation_positive_only_and_cached(pilot, make_mail, requests_mock):
    user, tracked = pilot
    mail = make_mail(tracked)
    submission, _ = posting.claim(mail.pk, user)
    submission.attempted_at = timezone.now() - timedelta(minutes=3)
    submission.save()
    lookup = requests_mock.get(f"https://zkillboard.com/api/killID/{mail.pk}/", json=[])
    assert posting.reconcile(submission).state == "unknown"
    assert posting.reconcile(submission).state == "unknown"
    assert lookup.call_count == 1
    cache.delete(f"killpusher:public:{mail.pk}")
    cache.delete("killpusher:zkill:request")
    requests_mock.get(
        f"https://zkillboard.com/api/killID/{mail.pk}/", json=[{"killmail_id": mail.pk}]
    )
    assert posting.reconcile(submission).state == "submitted"


def test_successful_post_does_not_follow_redirect(pilot, make_mail, requests_mock):
    _, tracked = pilot
    mail = make_mail(tracked)
    endpoint = requests_mock.post(
        post_url(mail), status_code=302, headers={"Location": "https://example.invalid/"}
    )
    assert clients.post_killmail(mail).state == "unknown"
    assert endpoint.call_count == 1
