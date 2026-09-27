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
from eve_sde.models import ItemType, SolarSystem

from . import posting
from .access import current_alliance, tracked_for, valid_token
from .conf import PERMISSION, SCOPE, cutoff
from .models import CharacterKillmail, Killmail, Submission, TrackedCharacter


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
                    (
                        f"Character {mail.victim_character_id}"
                        if mail.victim_character_id
                        else "NPC / structure"
                    ),
                ),
                "corporation": corps.get(
                    mail.victim_corporation_id,
                    (f"Corp {mail.victim_corporation_id}" if mail.victim_corporation_id else ""),
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
    messages.success(
        request, "Character connected. Killmails will import on the next five-minute poll."
    )
    return redirect("killpusher:index")


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
    # AA uses a shared Redis cache. Space outbound zKillboard requests across workers.
    if not cache.add("killpusher:zkill:request", True, timeout=1):
        return JsonResponse(
            {"message": "Please wait a second before posting another killmail."}, status=429
        )
    return submission_response(posting.submit(mail, request.user))


@login_required
@permission_required(PERMISSION, raise_exception=True)
@require_POST
@never_cache
def check_submission(request, killmail_id):
    visible_mail(request, killmail_id)
    submission = get_object_or_404(Submission, pk=killmail_id)
    if not cache.add("killpusher:zkill:request", True, timeout=1):
        return JsonResponse({"message": "Please wait a second before checking again."}, status=429)
    return submission_response(posting.reconcile(submission))
