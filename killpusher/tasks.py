from datetime import timedelta

from celery import shared_task
from django.core.cache import cache
from django.db.models import Q
from django.utils import timezone
from kombu.exceptions import OperationalError

from . import clients
from .conf import cutoff
from .enrichment import resolve_names
from .models import Killmail, Submission, TrackedCharacter
from .posting import mark_public
from .sync import sync_character


@shared_task
def poll_killmails():
    cleanup()
    enrich_killmails.apply_async(expires=300)
    now = timezone.now()
    due = (
        TrackedCharacter.objects.filter(token__isnull=False)
        .filter(
            Q(next_poll__isnull=True) | Q(next_poll__lte=now),
        )
        .values_list("pk", flat=True)
    )
    for pk in due.iterator():
        # A database lease prevents duplicate work even if Beat dispatches twice.
        fetch_character.apply_async(args=[pk], expires=300)


@shared_task(soft_time_limit=240, time_limit=270)
def fetch_character(character_pk):
    sync_character(character_pk)
    enrich_killmails.apply_async(expires=300)


@shared_task(soft_time_limit=55, time_limit=60)
def enrich_killmails():
    resolve_names()
    schedule_public_checks()


def schedule_public_checks():
    # One shared scheduling window, regardless of how many users import/refresh.
    if not cache.add("killpusher:zkill:batch", True, timeout=300):
        return
    now = timezone.now()
    due = Q(next_public_check__isnull=True) | Q(next_public_check__lte=now)
    terminal = Submission.objects.filter(state__in=["submitted", "sending"]).values("pk")
    candidates = list(
        Killmail.objects.filter(
            occurred_at__gte=cutoff(),
            occurred_at__lte=now - timedelta(minutes=5),
        )
        .filter(due)
        .exclude(pk__in=terminal)
        .order_by("next_public_check", "id")
        .values_list("pk", flat=True)[:20]
    )
    for index, pk in enumerate(candidates):
        if (
            not Killmail.objects.filter(pk=pk)
            .filter(due)
            .update(
                next_public_check=now + timedelta(hours=1),
            )
        ):
            continue
        try:
            check_public_killmail.apply_async(args=[pk], countdown=index * 10, expires=300)
        except OperationalError:
            Killmail.objects.filter(pk=pk).update(next_public_check=now + timedelta(minutes=5))


@shared_task(soft_time_limit=45, time_limit=50)
def check_public_killmail(killmail_id):
    mail = Killmail.objects.filter(pk=killmail_id, occurred_at__gte=cutoff()).first()
    if (
        mail is None
        or Submission.objects.filter(pk=killmail_id, state__in=["submitted", "sending"]).exists()
    ):
        return
    if mail.occurred_at > timezone.now() - timedelta(minutes=5):
        Killmail.objects.filter(pk=killmail_id).update(
            next_public_check=mail.occurred_at + timedelta(minutes=5),
        )
        return
    found = clients.is_public(killmail_id)
    if found is True:
        mark_public(killmail_id)
    else:
        Killmail.objects.filter(pk=killmail_id).update(
            next_public_check=timezone.now()
            + (timedelta(hours=1) if found is False else timedelta(minutes=5)),
        )


@shared_task
def cleanup():
    Killmail.objects.filter(occurred_at__lt=cutoff()).delete()
    # Only durable successful/uncertain claims need to survive the seven-day window.
    Submission.objects.filter(
        state=Submission.State.REJECTED,
        attempted_at__lt=cutoff(),
    ).delete()
    Submission.objects.filter(
        state=Submission.State.SENDING,
        attempted_at__lt=timezone.now() - timedelta(minutes=2),
    ).update(
        state=Submission.State.UNKNOWN, message="Posting interrupted. Push locked pending a check."
    )
