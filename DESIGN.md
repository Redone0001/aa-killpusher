# AA Killmail Pusher — design and field inventory

Research completed on 2026-09-27. The owner selected minimal storage, seven-day
retention, AA 5.4, and the user's current alliance. The implementation uses the
AA main character's current recorded alliance. See README.md for installation,
implemented behavior, and verification limits. The module is not deployed.

## Requested behavior

- An Alliance Auth menu entry and permission-protected page.
- Users authorize each of their characters through AA's existing EVE SSO flow
  with `esi-killmails.read_killmails.v1`. The app references AA/django-esi tokens;
  users do not paste access tokens and the module does not duplicate them.
- A paginated list of the user's kills and losses, with character, date, ship,
  system, victim, and submission status.
- A five-minute Celery Beat task discovers new killmails and imports selected
  fields. Fetching does not automatically publish anything.
- A push action posts to zKillboard without navigating away from the list.
- A friendly victim requires explicit confirmation. The server enforces this
  rule as well as the browser; no alliance must not equal no alliance.
- A successful submission disables pushing the same killmail across the entire
  installation, including when another user has access to that killmail.
- A copy button supplies the ESI killmail URL for manual posting on zKillboard.
  Copying is not proof that a killmail was posted and does not mark it submitted.
- Visibility and push permissions use current AA character ownership, never
  client-supplied character IDs alone.

## Verified upstream interfaces

The example module supplies the menu hook, URL hook, permission, and AA template
integration pattern. Its older Django imports should be modernized rather than
copied unchanged.

The current ESI OpenAPI schema documents:

- `GET /characters/{character_id}/killmails/recent`: paginated ID/hash pairs,
  going back 90 days, requiring `esi-killmails.read_killmails.v1`.
- A 300-second cache for that list; pagination is reported in `X-Pages`.
- `GET /killmails/{killmail_id}/{killmail_hash}`: the detailed killmail.
- Current character killmail rate-limit bucket: 30 tokens per 15 minutes.
  Import must honor rate-limit/cache headers and resume large backfills over
  multiple runs rather than assuming every page fits one polling cycle.

Character killmails normally cover losses and final-blow kills. They do not
provide a complete list of every assisted fleet kill. Other participating
characters can be identified from killmails that the installation has obtained,
but that does not make the source complete.

zKillboard's current JSON posting endpoint is
`POST https://zkillboard.com/api/killmail/add/{killID}/{hash}/`.
The request needs a descriptive User-Agent and compression support. Success
requires validating the response body, not merely receiving HTTP 200.
HTTP 408 can mean the killmail was accepted but processing timed out.

`django-eveonline-sde` provides `eve_sde.models.ItemType` and `SolarSystem`,
with ID and name fields. Use these for display, with an ID fallback if a local
SDE entry is missing. Character, corporation, and alliance names are dynamic
and need AA's entity data or a separate ESI name cache; they are not SDE data.

## Information available and proposed storage

| Group | Fields | Proposal |
| --- | --- | --- |
| Posting reference | Killmail ID and hash | Required while the detail is retained; derive copy URL |
| Time and location | UTC kill time, solar-system ID | Keep |
| Victim identity | Character, corporation, alliance IDs (when present) | Keep |
| Victim ship | Ship type ID | Keep; name from SDE |
| User involvement | Linked authorized character, kill/loss role | Keep; derived from payload |
| Friendly check | Victim alliance and comparison alliance | Keep enough to implement the selected rule |
| Submission audit | State, attempt time, success time, submitting user | Keep; generated locally |
| Sync health | Character, last success, next permitted poll, pagination progress, safe error category | Keep; generated locally |
| Combat summary | Attacker count, final-blow character/corp/alliance/ship, damage taken | Optional, small |
| All attackers | Character/corp/alliance/faction IDs, ship, weapon, damage, final-blow flag, security status | Optional, variable size |
| Fitting and cargo | Item types, location flags, dropped/destroyed quantities, singleton markers, nested contents | Optional, potentially large |
| Exact location | Victim x/y/z coordinates; optional moon ID | Optional |
| Other context | Optional war ID, victim faction ID | Optional |
| Raw payload | Full JSON killmail response | Optional, largest choice; avoid duplicating structured data unnecessarily |

There is no ISK valuation in the ESI killmail response. SDE does not supply
historical killmail market valuation either. Showing an ISK total would need
a separate price/valuation source and is outside the proposed minimum.

Storage profiles considered (minimal was selected):

1. **Minimal (recommended):** list, ownership, friendly-check, sync, and posting
   fields only. Discard all other payload fields after parsing.
2. **Summary:** minimal plus attacker count and final-blow pilot. Individual
   additional summary fields may be selected from the table.
3. **Full:** retain the complete killmail JSON in addition to required indexed
   fields. This is useful for local detailed kill reports but increases storage.

## Retention and duplicate prevention

Selected detail retention is seven days from kill time.
Deduplicate detail rows globally by killmail ID, with separate character links.
Apply the same age cutoff during import and cleanup, so polling cannot restore
expired details repeatedly.

Keep a separate compact, permanent submission record keyed by killmail ID when
details expire. This preserves the promise that a submitted killmail cannot be
pushed again through this module. This record grows slowly; an absolute fixed
database size and permanent duplicate prevention cannot both be guaranteed
without an external history store.

Posting should claim the killmail atomically before making the external request
to prevent double-clicks and concurrent users from creating duplicate requests.
Use states such as ready, sending, submitted, rejected, and outcome unknown.
An uncertain network result or interrupted worker must not automatically reopen
the push button: zKillboard may already have received it. Reconcile through a
read-only lookup where possible, with an explicit administrative recovery path
when the result cannot be established. The implementation keeps unresolved
outcomes locked and does not expose a reset action. Confirmed rejections may be retried.
The module can prevent repeat requests through itself; it cannot stop someone
from posting the copied URL elsewhere.

## Accepted decisions

1. Minimal fields only.
2. Detail retention: seven days.
3. Alliance Auth 5.4. Local verification uses django-esi 9.10.0.
4. Friendly comparison: the user's AA main character's current alliance versus
   the victim's recorded alliance in the killmail.

The proposed warning applies to losses as well when they match the selected
alliance rule, since posting either type exposes a friendly loss publicly.
No-alliance and unavailable-alliance cases must be handled separately.

## Planned verification

- AA permissions, ownership changes, and isolation between users.
- Token scope and character ownership checks at authorization and sync time.
- Loss/kill classification, optional NPC fields, friendly confirmation, and
  no-alliance handling.
- Pagination, partial imports, rate limiting, revoked tokens, repeated polls,
  and overlap between scheduled runs.
- Posting success, already-present responses, explicit rejection, unknown
  outcomes, and concurrent attempts.
- Cleanup preserving duplicate prevention and avoiding reimport loops.
- Browser flow: confirmation, in-place status update, copy fallback, and errors.
- Django migration checks and package build against the selected AA version.

## Sources

- [Example AA app](https://github.com/ErikKalkoken/allianceauth_example)
- [Current ESI OpenAPI schema](https://esi.evetech.net/meta/openapi.json)
- [CCP killmail guidance](https://support.eveonline.com/hc/en-us/articles/11730655033884-Killmails)
- [Current zKillboard API](https://zkillboard.com/api/docs/)
- [Original posting wiki](https://github.com/zKillboard/zKillboard/wiki/API-(Posting-Killmails))
- [Django EVE SDE](https://pypi.org/project/django-eveonline-sde/)
- Public wheels inspected: Alliance Auth 5.4.0, django-esi 9.10.0,
  django-eveonline-sde 0.2.0. These are research references, not yet a selected
  compatibility matrix for this module.
