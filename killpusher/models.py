from django.conf import settings
from django.db import models


class TrackedCharacter(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    owner_hash = models.CharField(max_length=254)
    ownership = models.OneToOneField(
        "authentication.CharacterOwnership",
        on_delete=models.CASCADE,
        related_name="killpusher_character",
    )
    token = models.ForeignKey("esi.Token", on_delete=models.SET_NULL, null=True)
    last_sync = models.DateTimeField(null=True, blank=True)
    next_poll = models.DateTimeField(null=True, blank=True, db_index=True)
    next_page = models.PositiveIntegerField(default=1)
    sync_lock = models.UUIDField(null=True, blank=True)
    lock_until = models.DateTimeField(null=True, blank=True)
    error = models.CharField(max_length=160, blank=True)

    class Meta:
        default_permissions = ()
        permissions = [("basic_access", "Can use Killmail Pusher")]

    def __str__(self):
        return self.ownership.character.character_name


class Killmail(models.Model):
    id = models.PositiveBigIntegerField(primary_key=True)
    hash = models.CharField(max_length=40)
    occurred_at = models.DateTimeField(db_index=True)
    solar_system_id = models.PositiveBigIntegerField()
    victim_character_id = models.PositiveBigIntegerField(null=True)
    victim_corporation_id = models.PositiveBigIntegerField(null=True)
    victim_alliance_id = models.PositiveBigIntegerField(null=True)
    victim_ship_type_id = models.PositiveBigIntegerField()

    class Meta:
        default_permissions = ()
        ordering = ["-occurred_at", "-id"]

    @property
    def esi_url(self):
        return f"https://esi.evetech.net/killmails/{self.pk}/{self.hash}/"

    @property
    def zkill_url(self):
        return f"https://zkillboard.com/kill/{self.pk}/"


class CharacterKillmail(models.Model):
    class Role(models.TextChoices):
        KILL = "kill", "Kill"
        LOSS = "loss", "Loss"

    character = models.ForeignKey(TrackedCharacter, on_delete=models.CASCADE)
    killmail = models.ForeignKey(Killmail, on_delete=models.CASCADE, related_name="involvements")
    role = models.CharField(max_length=4, choices=Role.choices)

    class Meta:
        default_permissions = ()
        constraints = [
            models.UniqueConstraint(fields=["character", "killmail"], name="kp_character_kill")
        ]


class Submission(models.Model):
    """Permanent, compact ledger: deliberately no FK to expiring killmail details."""

    class State(models.TextChoices):
        SENDING = "sending", "Sending"
        SUBMITTED = "submitted", "Pushed"
        REJECTED = "rejected", "Rejected — retry available"
        UNKNOWN = "unknown", "Outcome unknown — push locked"

    killmail_id = models.PositiveBigIntegerField(primary_key=True)
    state = models.CharField(
        max_length=12,
        choices=State.choices,
        default=State.SENDING,
        db_index=True,
    )
    attempted_at = models.DateTimeField()
    submitted_at = models.DateTimeField(null=True, blank=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    message = models.CharField(max_length=160, blank=True)

    class Meta:
        default_permissions = ("view",)
