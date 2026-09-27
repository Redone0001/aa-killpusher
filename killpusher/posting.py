from datetime import timedelta

from django.core import signing
from django.db import transaction
from django.utils import timezone

from . import clients
from .access import current_alliance
from .models import Submission

SALT = "killpusher.friendly-confirmation"


def confirmation_needed(user, killmail, confirmation):
    alliance_id = current_alliance(user)
    if not alliance_id or alliance_id != killmail.victim_alliance_id:
        return None
    value = {"user": user.pk, "killmail": killmail.pk, "alliance": alliance_id}
    if confirmation:
        try:
            if signing.loads(confirmation, salt=SALT, max_age=300) == value:
                return None
        except signing.BadSignature:
            pass
    return signing.dumps(value, salt=SALT)


def claim(killmail_id, user):
    """Commit a global claim before the network call; never hold a DB lock over HTTP."""
    with transaction.atomic():
        submission, created = Submission.objects.get_or_create(
            killmail_id=killmail_id,
            defaults={"attempted_at": timezone.now(), "submitted_by": user},
        )
        if not created:
            submission = Submission.objects.select_for_update().get(pk=killmail_id)
            if submission.state != Submission.State.REJECTED:
                return submission, False
        submission.state = Submission.State.SENDING
        submission.attempted_at = timezone.now()
        submission.submitted_by = user
        submission.message = "Sending to zKillboard."
        submission.save()
        return submission, True


def submit(killmail, user):
    submission, claimed = claim(killmail.pk, user)
    if not claimed:
        return submission
    result = clients.post_killmail(killmail)
    submission.state = result.state
    submission.message = result.message
    if result.state == Submission.State.SUBMITTED:
        submission.submitted_at = timezone.now()
    submission.save(update_fields=["state", "message", "submitted_at"])
    return submission


def reconcile(submission):
    if submission.state == Submission.State.SENDING:
        if submission.attempted_at > timezone.now() - timedelta(minutes=2):
            return submission
        Submission.objects.filter(pk=submission.pk, state=Submission.State.SENDING).update(
            state=Submission.State.UNKNOWN,
            message="Posting was interrupted. Push locked pending a check.",
        )
        submission.refresh_from_db()
    if submission.state == Submission.State.UNKNOWN and clients.is_public(submission.pk):
        Submission.objects.filter(pk=submission.pk, state=Submission.State.UNKNOWN).update(
            state=Submission.State.SUBMITTED,
            submitted_at=timezone.now(),
            message="Confirmed present on zKillboard.",
        )
        submission.refresh_from_db()
    return submission
