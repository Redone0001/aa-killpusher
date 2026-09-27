from datetime import timedelta

from celery import shared_task
from django.db.models import Q
from django.utils import timezone

from .conf import cutoff
from .models import Killmail, Submission, TrackedCharacter
from .sync import sync_character


@shared_task
def poll_killmails():
    cleanup()
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
