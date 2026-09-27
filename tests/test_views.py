from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth.models import Permission
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from eve_sde.models import ItemType, SolarSystem

from killpusher.models import CharacterKillmail, Submission, TrackedCharacter

pytestmark = pytest.mark.django_db


def test_list_uses_sde_and_is_private(client, pilot, make_character, make_mail):
    user, tracked = pilot
    _, other = make_character(2)
    mail = make_mail(tracked)
    make_mail(other, mail_id=987654321)
    ItemType.objects.create(id=587, name="Rifter")
    SolarSystem.objects.create(id=30000142, name="Jita")
    client.force_login(user)
    response = client.get(reverse("killpusher:index"))
    assert response.status_code == 200
    body = response.content.decode()
    assert "Rifter" in body and "Jita" in body
    assert mail.esi_url in body
    assert "987654321" not in body
    assert "killpusher/killpusher.js" in body
    assert "no-store" in response.headers["Cache-Control"]


def test_expired_mails_hidden_even_before_cleanup(client, pilot, make_mail):
    user, tracked = pilot
    mail = make_mail(tracked, occurred=timezone.now() - timedelta(days=8))
    client.force_login(user)
    response = client.get(reverse("killpusher:index"))
    assert mail.esi_url not in response.content.decode()
    assert client.post(reverse("killpusher:push", args=[mail.pk])).status_code == 404


def test_other_user_cannot_push_or_check(client, pilot, make_character, make_mail):
    _, tracked = pilot
    user, _ = make_character(2)
    mail = make_mail(tracked)
    client.force_login(user)
    for action in ("push", "check"):
        assert client.post(reverse(f"killpusher:{action}", args=[mail.pk])).status_code == 404
    assert not Submission.objects.exists()


def test_login_permission_and_post_required(client, pilot, make_mail):
    user, tracked = pilot
    mail = make_mail(tracked)
    assert client.get(reverse("killpusher:index")).status_code == 302
    client.force_login(user)
    assert client.get(reverse("killpusher:push", args=[mail.pk])).status_code == 405
    user.user_permissions.remove(Permission.objects.get(codename="basic_access"))
    assert client.get(reverse("killpusher:index")).status_code == 403
    assert client.post(reverse("killpusher:push", args=[mail.pk])).status_code == 403


def test_csrf_is_enforced(pilot, make_mail):
    user, tracked = pilot
    mail = make_mail(tracked)
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    assert client.post(reverse("killpusher:push", args=[mail.pk])).status_code == 403


def test_character_transfer_does_not_leak_old_mails(client, pilot, make_character, make_mail):
    old_user, tracked = pilot
    new_user, _ = make_character(2)
    mail = make_mail(tracked)
    tracked.ownership.user = new_user
    tracked.ownership.save()
    for user in [old_user, new_user]:
        client.force_login(user)
        assert mail.esi_url not in client.get(reverse("killpusher:index")).content.decode()
        assert client.post(reverse("killpusher:push", args=[mail.pk])).status_code == 404


def test_connect_validates_sso_character_ownership(client, pilot, make_character):
    user, tracked = pilot
    _, other = make_character(2)
    client.force_login(user)
    with patch("esi.decorators._check_callback", return_value=other.token):
        assert client.get(reverse("killpusher:connect")).status_code == 403
    with patch("esi.decorators._check_callback", return_value=tracked.token):
        assert client.get(reverse("killpusher:connect")).status_code == 302


def test_disconnect_removes_only_local_tracking(client, pilot, make_mail):
    user, tracked = pilot
    make_mail(tracked)
    token = tracked.token
    client.force_login(user)
    assert client.post(reverse("killpusher:disconnect", args=[tracked.pk])).status_code == 302
    assert not TrackedCharacter.objects.exists()
    assert not CharacterKillmail.objects.exists()
    assert token.__class__.objects.filter(pk=token.pk).exists()


def test_loss_filter(client, pilot, make_mail):
    user, tracked = pilot
    loss = make_mail(tracked, role="loss")
    kill = make_mail(tracked, mail_id=987654321)
    client.force_login(user)
    body = client.get(reverse("killpusher:index"), {"role": "loss"}).content.decode()
    assert loss.esi_url in body
    assert kill.esi_url not in body
