from allianceauth.authentication.models import CharacterOwnership
from allianceauth.eveonline.models import EveCharacter, EveCorporationInfo
from django.contrib import messages
from django.contrib.auth.decorators import login_required, permission_required
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import transaction
from django.http import HttpResponseForbidden, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST
from esi.decorators import token_required
from esi.models import Token
from eve_sde.models import ItemType, SolarSystem

from . import posting
from .access import current_alliance, tracked_for, valid_token
from .conf import PERMISSION, SCOPE, cutoff
from .models import CharacterKillmail, Killmail, Submission, TrackedCharacter
from .queueing import queue_import


def visible_links(user):
    return CharacterKillmail.objects.filter(
        character__in=tracked_for(user),
        killmail__occurred_at__gte=cutoff(),
    )


def names(model, ids, id_field="pk", name_field="name"):
    return dict(model.objects.filter(**{f"{id_field}__in": ids}).values_list(id_field, name_field))


@login_required
@permission_required(PERMISSION, raise_exception=True)
@never_cache
def index(request):
    links = (
        visible_links(request.user)
        .select_related(
            "killmail",
            "character__ownership__character",
        )
        .order_by("-killmail__occurred_at", "-killmail_id", "pk")
    )
    role = request.GET.get("role", "")
    if role in ("kill", "loss"):
        links = links.filter(role=role)
    else:
        role = ""
    pushed = request.GET.get("pushed", "")
    submitted_ids = Submission.objects.filter(state=Submission.State.SUBMITTED).values(
        "killmail_id"
    )
    if pushed == "yes":
        links = links.filter(killmail_id__in=submitted_ids)
    elif pushed == "no":
        links = links.exclude(killmail_id__in=submitted_ids)
    else:
        pushed = ""
    page = Paginator(links, 50).get_page(request.GET.get("page"))
    mails = [link.killmail for link in page]
    ships = names(ItemType, {m.victim_ship_type_id for m in mails})
    systems = names(SolarSystem, {m.solar_system_id for m in mails})
    pilots = names(
        EveCharacter,
        {m.victim_character_id for m in mails},
        "character_id",
        "character_name",
    )
    corps = names(
        EveCorporationInfo,
        {m.victim_corporation_id for m in mails},
        "corporation_id",
        "corporation_name",
    )
    submissions = Submission.objects.in_bulk([m.pk for m in mails])
    alliance_id = current_alliance(request.user)
    rows = []
    for link in page:
        mail = link.killmail
        submission = submissions.get(mail.pk)
        rows.append(
            {
                "link": link,
                "mail": mail,
                "ship": ships.get(mail.victim_ship_type_id, f"Type {mail.victim_ship_type_id}"),
                "system": systems.get(mail.solar_system_id, f"System {mail.solar_system_id}"),
                "victim": pilots.get(
                    mail.victim_character_id,
                    mail.victim_character_name
                    or (
                        f"Character {mail.victim_character_id}"
                        if mail.victim_character_id
                        else "NPC / structure"
                    ),
                ),
                "corporation": corps.get(
                    mail.victim_corporation_id,
                    mail.victim_corporation_name
                    or (f"Corp {mail.victim_corporation_id}" if mail.victim_corporation_id else ""),
                ),
                "friendly": bool(alliance_id and mail.victim_alliance_id == alliance_id),
                "submission": submission,
                "can_push": submission is None or submission.state == Submission.State.REJECTED,
                "can_check": submission and submission.state in ("sending", "unknown"),
            }
        )
    return render(
        request,
        "killpusher/index.html",
        {
            "rows": rows,
            "page": page,
            "role": role,
            "pushed": pushed,
            "characters": tracked_for(request.user),
            "alliance_id": alliance_id,
        },
    )


@login_required
@permission_required(PERMISSION, raise_exception=True)
@token_required(scopes=[SCOPE], new=True)
def connect(request, token):
    ownership = CharacterOwnership.objects.filter(
        user=request.user,
        character__character_id=token.character_id,
        owner_hash=token.character_owner_hash,
    ).first()
    if (
        ownership is None
        or token.user_id != request.user.pk
        or not token.scopes.filter(name=SCOPE).exists()
    ):
        return HttpResponseForbidden(
            "Add this character to your AA account first, then reconnect it here."
        )
    with transaction.atomic():
        character, _ = TrackedCharacter.objects.get_or_create(
            ownership=ownership,
            defaults={"user": request.user, "owner_hash": ownership.owner_hash},
        )
        character = TrackedCharacter.objects.select_for_update().get(pk=character.pk)
        if character.user_id != request.user.pk or character.owner_hash != ownership.owner_hash:
            # Never inherit a previous owner's local killmail access or sync state.
            CharacterKillmail.objects.filter(character=character).delete()
            character.user = request.user
            character.owner_hash = ownership.owner_hash
            character.last_sync = None
            character.next_page = 1
        character.token = token
        if not valid_token(character):
            return HttpResponseForbidden("This token does not match your AA character ownership.")
        character.error = ""
        # Invalidate an old worker's lease, so a late authorization failure cannot
        # overwrite this freshly granted token in its finally block.
        character.sync_lock = None
        character.lock_until = None
        character.save()
        transaction.on_commit(lambda pk=character.pk: queue_import(pk))
    messages.success(
        request, "Character connected. Import requested; any existing CCP cooldown still applies."
    )
    return redirect("killpusher:index")


@login_required
@permission_required(PERMISSION, raise_exception=True)
@require_POST
@never_cache
def import_characters(request):
    if not cache.add(f"killpusher:discover:{request.user.pk}", True, timeout=60):
        return JsonResponse({"message": "Please wait a minute before importing again."}, status=429)
    connected = skipped = missing = 0
    with transaction.atomic():
        ownerships = CharacterOwnership.objects.select_for_update().filter(user=request.user)
        for ownership in ownerships:
            character = (
                TrackedCharacter.objects.select_for_update().filter(ownership=ownership).first()
            )
            if character and valid_token(character):
                skipped += 1
                continue
            token = (
                Token.objects.filter(
                    user=request.user,
                    character_id=ownership.character.character_id,
                    character_owner_hash=ownership.owner_hash,
                    scopes__name=SCOPE,
                )
                .order_by("-pk")
                .first()
            )
            if token is None:
                missing += 1
                continue
            if character is None:
                character = TrackedCharacter(ownership=ownership)
            elif (
                character.user_id != request.user.pk or character.owner_hash != ownership.owner_hash
            ):
                CharacterKillmail.objects.filter(character=character).delete()
                character.last_sync = None
                character.next_page = 1
            character.user = request.user
            character.owner_hash = ownership.owner_hash
            character.token = token
            character.error = ""
            character.sync_lock = None
            character.lock_until = None
            character.save()
            transaction.on_commit(lambda pk=character.pk: queue_import(pk))
            connected += 1
    return JsonResponse(
        {
            "connected": connected,
            "skipped": skipped,
            "missing": missing,
            "message": (
                f"Connected {connected} character(s); import requested. "
                f"{skipped} already connected. {missing} need killmail access "
                "through Connect a character. "
                "The import worker checks existing tokens; revoked tokens require reconnection. "
                "Reload to see updated characters and killmails."
            ),
        },
        status=202,
    )


@login_required
@permission_required(PERMISSION, raise_exception=True)
@require_POST
@never_cache
def refresh_all(request):
    if not cache.add(f"killpusher:refresh:{request.user.pk}", True, timeout=60):
        return JsonResponse(
            {"message": "Please wait a minute before requesting another refresh."}, status=429
        )
    characters = [character for character in tracked_for(request.user) if valid_token(character)]
    if not characters:
        cache.delete(f"killpusher:refresh:{request.user.pk}")
        return JsonResponse(
            {"message": "Connect a character with killmail access before refreshing."}, status=400
        )
    queued = sum(queue_import(character.pk) for character in characters)
    failed = len(characters) - queued
    if failed:
        # Allow retry after a broker failure; any accepted jobs remain protected by the DB lease.
        cache.delete(f"killpusher:refresh:{request.user.pk}")
    message = (
        f"Import requested for {queued} character(s). CCP cooldowns still apply. "
        "Reload the list after imports finish."
    )
    if failed:
        message += f" {failed} could not be queued; the scheduled poll will retry."
    return JsonResponse(
        {"queued": queued, "failed": failed, "message": message}, status=202 if queued else 503
    )


@login_required
@permission_required(PERMISSION, raise_exception=True)
@require_POST
def disconnect(request, character_pk):
    with transaction.atomic():
        character = get_object_or_404(
            tracked_for(request.user).select_related(None).select_for_update(), pk=character_pk
        )
        character.delete()
    messages.success(request, "Character disconnected from Killmail Pusher.")
    return redirect("killpusher:index")


def visible_mail(request, killmail_id):
    return get_object_or_404(
        Killmail.objects.filter(involvements__in=visible_links(request.user)).distinct(),
        pk=killmail_id,
    )


def submission_response(submission):
    return JsonResponse(
        {
            "state": submission.state,
            "label": submission.get_state_display(),
            "message": submission.message,
            "can_push": submission.state == Submission.State.REJECTED,
            "can_check": submission.state in (Submission.State.SENDING, Submission.State.UNKNOWN),
        }
    )


@login_required
@permission_required(PERMISSION, raise_exception=True)
@require_POST
@never_cache
def push(request, killmail_id):
    mail = visible_mail(request, killmail_id)
    previous = Submission.objects.filter(pk=mail.pk).first()
    if previous and previous.state != Submission.State.REJECTED:
        return submission_response(previous)
    confirmation = posting.confirmation_needed(request.user, mail, request.POST.get("confirmation"))
    if confirmation:
        return JsonResponse(
            {
                "state": "confirmation_required",
                "confirmation": confirmation,
                "message": "The victim belongs to your main character’s current alliance. "
                "Publish this friendly loss to zKillboard?",
            },
            status=409,
        )
    return submission_response(posting.submit(mail, request.user))


@login_required
@permission_required(PERMISSION, raise_exception=True)
@require_POST
@never_cache
def check_submission(request, killmail_id):
    visible_mail(request, killmail_id)
    submission = get_object_or_404(Submission, pk=killmail_id)
    return submission_response(posting.reconcile(submission))
