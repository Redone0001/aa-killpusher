from datetime import timedelta
from unittest.mock import patch

import pytest
import requests
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone

from killpusher import clients, posting
from killpusher.enrichment import resolve_names
from killpusher.models import Killmail, Submission
from killpusher.tasks import check_public_killmail, schedule_public_checks

pytestmark = pytest.mark.django_db


def old_mail(pilot, make_mail, mail_id=123456789):
    return make_mail(pilot[1], mail_id=mail_id, occurred=timezone.now() - timedelta(hours=1))


def test_existing_public_mail_marked_pushed_without_post(client, pilot, make_mail, requests_mock):
    mail = old_mail(pilot, make_mail)
    requests_mock.get(
        f"https://zkillboard.com/api/killID/{mail.pk}/", json=[{"killmail_id": mail.pk}]
    )
    check_public_killmail(mail.pk)
    ledger = Submission.objects.get(pk=mail.pk)
    assert ledger.state == "submitted"
    assert ledger.submitted_by is None
    assert ledger.message == "Already present on zKillboard."
    client.force_login(pilot[0])
    response = client.post(reverse("killpusher:push", args=[mail.pk]))
    assert response.json()["state"] == "submitted"
    assert not response.json()["can_push"]
    assert requests_mock.call_count == 1
    assert requests_mock.last_request.method == "GET"


def test_negative_result_does_not_mark_pushed_and_is_cached(pilot, make_mail, requests_mock):
    mail = old_mail(pilot, make_mail)
    endpoint = requests_mock.get(f"https://zkillboard.com/api/killID/{mail.pk}/", json=[])
    check_public_killmail(mail.pk)
    assert not Submission.objects.exists()
    assert clients.is_public(mail.pk) is False
    assert endpoint.call_count == 1
    mail.refresh_from_db()
    assert mail.next_public_check > timezone.now() + timedelta(minutes=59)


@pytest.mark.parametrize(
    "status,body", [(429, {}), (500, {}), (200, {"error": "bad"}), (200, [{}])]
)
def test_failed_lookup_is_unknown_not_cached_as_absent(
    pilot, make_mail, requests_mock, status, body
):
    mail = old_mail(pilot, make_mail)
    requests_mock.get(
        f"https://zkillboard.com/api/killID/{mail.pk}/",
        json=body,
        status_code=status,
        headers={"Retry-After": "600"},
    )
    assert clients.is_public(mail.pk) is None
    assert cache.get(f"killpusher:public:{mail.pk}") is None
    assert cache.get("killpusher:zkill:backoff")
    assert clients.is_public(mail.pk + 1) is None
    assert requests_mock.call_count == 1
    assert not Submission.objects.exists()


def test_network_error_sets_shared_backoff(pilot, make_mail, requests_mock):
    mail = old_mail(pilot, make_mail)
    requests_mock.get(f"https://zkillboard.com/api/killID/{mail.pk}/", exc=requests.Timeout)
    assert clients.is_public(mail.pk) is None
    assert clients.post_killmail(mail).state == "rejected"
    assert requests_mock.call_count == 1


def test_shared_request_spacing_between_reads_and_posts(pilot, make_mail, requests_mock):
    mail = old_mail(pilot, make_mail)
    requests_mock.get(f"https://zkillboard.com/api/killID/{mail.pk}/", json=[])
    assert clients.is_public(mail.pk) is False
    assert clients.is_public(mail.pk + 1) is None
    assert clients.post_killmail(mail).state == "rejected"
    assert requests_mock.call_count == 1


def test_scheduler_is_bounded_and_shared(pilot, make_mail):
    for number in range(25):
        old_mail(pilot, make_mail, mail_id=123456789 + number)
    with patch("killpusher.tasks.check_public_killmail.apply_async") as enqueue:
        schedule_public_checks()
        schedule_public_checks()
    assert enqueue.call_count == 20
    assert [call.kwargs["countdown"] for call in enqueue.call_args_list] == list(range(0, 200, 10))
    assert Killmail.objects.filter(next_public_check__isnull=False).count() == 20


def test_scheduler_skips_fresh_expired_and_already_pushed(pilot, make_mail):
    make_mail(pilot[1])
    make_mail(pilot[1], mail_id=2, occurred=timezone.now() - timedelta(days=8))
    published = old_mail(pilot, make_mail, mail_id=3)
    posting.mark_public(published.pk)
    with patch("killpusher.tasks.check_public_killmail.apply_async") as enqueue:
        schedule_public_checks()
        enqueue.assert_not_called()


def test_confirmed_public_state_survives_racing_failed_post(pilot, make_mail):
    mail = old_mail(pilot, make_mail)

    def racing_lookup(_mail):
        posting.mark_public(mail.pk)
        return clients.PostResult("unknown", "timeout")

    with patch("killpusher.clients.post_killmail", side_effect=racing_lookup):
        result = posting.submit(mail, pilot[0])
    assert result.state == "submitted"


def test_names_resolved_in_one_bulk_call_and_shown(client, pilot, make_mail, requests_mock):
    mail = old_mail(pilot, make_mail)
    endpoint = requests_mock.post(
        "https://esi.evetech.net/universe/names",
        json=[
            {"id": mail.victim_character_id, "name": "Victim Pilot", "category": "character"},
            {
                "id": mail.victim_corporation_id,
                "name": "Victim Corporation",
                "category": "corporation",
            },
        ],
    )
    resolve_names()
    mail.refresh_from_db()
    assert mail.victim_character_name == "Victim Pilot"
    assert mail.victim_corporation_name == "Victim Corporation"
    assert set(endpoint.last_request.json()) == {
        mail.victim_character_id,
        mail.victim_corporation_id,
    }
    assert "Authorization" not in endpoint.last_request.headers
    client.force_login(pilot[0])
    body = client.get(reverse("killpusher:index")).content.decode()
    assert "Victim Pilot" in body and "Victim Corporation" in body
    assert endpoint.call_count == 1


def test_name_cache_reused_for_new_killmails(pilot, make_mail, requests_mock):
    first = old_mail(pilot, make_mail)
    requests_mock.post(
        "https://esi.evetech.net/universe/names",
        json=[
            {"id": first.victim_character_id, "name": "Victim", "category": "character"},
            {"id": first.victim_corporation_id, "name": "Corp", "category": "corporation"},
        ],
    )
    resolve_names()
    second = old_mail(pilot, make_mail, mail_id=2)
    cache.delete("killpusher:names:batch")
    resolve_names()
    second.refresh_from_db()
    assert second.victim_character_name == "Victim"
    assert second.victim_corporation_name == "Corp"
    assert requests_mock.call_count == 1


def test_name_failure_keeps_mails_and_retries_later(pilot, make_mail, requests_mock):
    mail = old_mail(pilot, make_mail)
    requests_mock.post("https://esi.evetech.net/universe/names", status_code=503)
    resolve_names()
    mail.refresh_from_db()
    assert mail.victim_character_name == ""
    assert mail.next_name_check > timezone.now()
    assert Killmail.objects.filter(pk=mail.pk).exists()


def test_missing_names_do_not_starve_later_rows(pilot, make_mail, requests_mock):
    for number in range(101):
        old_mail(pilot, make_mail, mail_id=number + 1)
    requests_mock.post("https://esi.evetech.net/universe/names", status_code=503)
    resolve_names()
    assert Killmail.objects.filter(next_name_check__isnull=False).count() == 100
    cache.delete("killpusher:names:batch")
    resolve_names()
    assert Killmail.objects.filter(next_name_check__isnull=False).count() == 101


def test_shorter_backoff_does_not_replace_retry_after():
    clients.pause_zkill(600)
    until = cache.get("killpusher:zkill:backoff")
    clients.pause_zkill(60)
    assert cache.get("killpusher:zkill:backoff") == until


def test_positive_ledger_survives_cleanup(pilot, make_mail):
    from killpusher.tasks import cleanup

    mail = old_mail(pilot, make_mail)
    posting.mark_public(mail.pk)
    Killmail.objects.filter(pk=mail.pk).update(occurred_at=timezone.now() - timedelta(days=8))
    cleanup()
    assert not Killmail.objects.filter(pk=mail.pk).exists()
    assert Submission.objects.get(pk=mail.pk).state == "submitted"


def test_known_aa_name_needs_no_lookup(pilot, make_mail):
    from allianceauth.eveonline.models import EveCorporationInfo

    mail = old_mail(pilot, make_mail)
    mail.victim_character_id = pilot[1].ownership.character.character_id
    mail.save()
    EveCorporationInfo.objects.create(
        corporation_id=mail.victim_corporation_id,
        corporation_name="Known Corp",
        corporation_ticker="KNOWN",
        member_count=1,
    )
    with patch("killpusher.enrichment.EsiClient.names") as lookup:
        resolve_names()
        lookup.assert_not_called()
    mail.refresh_from_db()
    assert mail.victim_character_name == "Pilot 1"
    assert mail.victim_corporation_name == "Known Corp"
