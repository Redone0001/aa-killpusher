# AA Killmail Pusher

An Alliance Auth **5.4** module for deliberately publishing character killmails
to zKillboard. Based on the integration pattern in
[allianceauth_example](https://github.com/ErikKalkoken/allianceauth_example),
with modern AA/Django hooks and templates.

## What users get

- Connect any character already owned by their AA account using EVE SSO;
  an initial import is queued as soon as authorization is saved.
- **Refresh all characters** requests imports for every character connected to
  this module on the current account, while respecting CCP cooldowns.
- List the last **7 days** of imported kills and losses, with a kill/loss filter
  and 50 character entries per page.
- Push a killmail without navigating away from the page.
- Confirm before publishing a victim from the **current AA main character's
  alliance**. This applies to matching losses too. Two unaffiliated characters
  are not treated as alliance mates. AA's stored main-character affiliation is
  used at click time; its freshness follows AA's character updates.
- See a persistent **Pushed** state and no repeat push after success, shared
  across every user who has access to that killmail.
- Killmails already public on zKillboard are automatically marked **Pushed**,
  even when someone else posted them. Checks run in the background.
- Victim pilot and corporation names are resolved through AA and CCP, with IDs
  used temporarily while a lookup is pending or unavailable.
- Copy its ESI URL for [manual zKillboard posting](https://zkillboard.com/post/).
- Check ambiguous submissions against the public zKillboard API.
- Disconnect this module's character tracking without deleting tokens used by
  other AA applications.

An import never automatically publishes killmails. Only the user's push action
sends them to zKillboard. Only currently owned, explicitly connected characters'
imported mails are visible to the user.

## Installation on AA 5.4

Repository: [Redone0001/aa-killpusher](https://github.com/Redone0001/aa-killpusher).

Python 3.10+ is required. Run the installation inside the same Python virtual
environment/container as AA. Clone using a GitHub account with repository access:

```sh
gh auth login --hostname github.com
gh repo clone Redone0001/aa-killpusher
cd aa-killpusher
git checkout v0.1.3
python -m pip install .
```

GitHub authentication is needed for a private repository. Do not place access
tokens in install URLs, shell history, or settings files. If the repository is
public, you can instead install the tagged version directly:

```sh
python -m pip install 'git+https://github.com/Redone0001/aa-killpusher.git@v0.1.3'
```

Alternatively, install a built wheel:

```sh
pip install /path/to/aa_killpusher-0.1.3-py3-none-any.whl
```

The package is not published on PyPI. The wheel is generated under `dist/` when
building this checkout. After installing, switch back to your AA project folder
(the one containing `manage.py`) for the following configuration and commands.

Your existing `django-eveonline-sde` installation must have `eve_sde` enabled,
its SDE imported, and `modeltranslation` configured as documented upstream.
This module uses `ItemType` and `SolarSystem`; missing entries display their IDs
instead of preventing the page from loading.

Add to your AA `local.py`:

```python
from celery.schedules import crontab

INSTALLED_APPS += ["killpusher.apps.KillpusherConfig"]

CELERYBEAT_SCHEDULE["Killmail Pusher :: Import and cleanup"] = {
    "task": "killpusher.tasks.poll_killmails",
    "schedule": crontab(minute="*/5"),
}
```

The HTTP User-Agent is populated automatically from AA's existing `SITE_URL`
and `ESI_USER_CONTACT_EMAIL` settings, together with the module version. No
module-specific contact setting is required. AA retains responsibility for
validating its central settings. Existing `KILLPUSHER_USER_AGENT` overrides
remain supported, but can be removed to use the shared AA configuration.

To upgrade an existing installation:

```sh
python -m pip install --upgrade 'git+https://github.com/Redone0001/aa-killpusher.git@v0.1.3'
python manage.py migrate
python manage.py check
python manage.py collectstatic --noinput
```

Restart AA's web process and Celery workers after upgrading. Version 0.1.3
requires migration `0002`, which adds cached victim names and background lookup
timing fields. No additional Beat schedule or ESI scope is needed. Existing
seven-day killmails are checked and enriched by the existing polling schedule.

Add **`esi-killmails.read_killmails.v1`** to the allowed scopes of the EVE
developer application already used by AA. Keep AA's existing SSO callback URL.
There is no separate OAuth client or callback to configure. Each user consents
to the scope through the module's **Connect a character** button; it is not
necessary to add this scope to AA's default login scopes.

Then run your usual AA deployment commands:

```sh
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py check
```

Restart AA's web process, Celery workers, and Celery Beat using your deployment's
normal service/container workflow. Run only one Beat scheduler for the instance.
Use AA's shared Redis cache and a production database supported by AA.

Grant **Killmail Pusher → Can use Killmail Pusher**
(`killpusher.basic_access`) to the intended users, groups, or states. The menu
entry and `/killpusher/` page then become available.

Connect a character, allow its queued import to finish, and review its import
status in **Connected characters and import status**. Reconnecting also requests
an import. These tasks are queued after the authorization transaction commits,
so workers can see the new token and tracking record.

**Refresh all characters** requests imports for all of your characters connected
to this module with valid ownership and killmail scopes. Characters merely linked
to AA still need to authorize the module first. The request runs in the background;
use **Reload list and import status** after the imports finish. Repeated refresh
requests are limited to once per minute per account. Existing five-minute polling
cooldowns, active imports, and longer CCP rate-limit backoffs are respected:
manual refresh does not force a fresh response out of CCP's cache. If the task
broker is unavailable, authorization is retained and the next scheduled poll can
retry once it recovers.

For initial diagnosis,
the regular task can also be dispatched from the AA shell:

```python
from killpusher.tasks import poll_killmails
poll_killmails.delay()
```

## Stored data

The selected minimal profile stores:

| Data | Retention |
| --- | --- |
| Killmail ID/hash, time, system ID | 7 days from kill time |
| Victim character/corporation/alliance/ship IDs | 7 days from kill time |
| Resolved victim character/corporation names and lookup timing | With the killmail |
| Character association and kill/loss role | With the killmail, or until disconnect |
| Tracking, authorization reference, poll progress and safe error message | While connected |
| Submission ID, state, user reference, timestamps and brief result | Successful/uncertain records permanently; rejected records 7 days from attempt |

Full attacker lists, damage, fitting, cargo, coordinates, and raw killmail JSON
are **not stored**. Ship and system names are looked up in your existing SDE.
Victim pilot/corporation names first use existing AA records. A background job
resolves missing names with CCP's public bulk `/universe/names` endpoint, at
most 200 unique IDs (100 killmails) per batch and one batch per minute across
the installation. Shared name-cache entries expire after seven days; names on
killmail rows are deleted with those rows. Failed/missing name results are
retried after five minutes. Page rendering makes no external lookup requests.

Cleanup runs with every five-minute import task. Old details are also excluded
from the page and push endpoint immediately, even if a cleanup run is delayed.
Import ignores mails older than seven days, so expired details stay expired.
The compact permanent posting ledger has no foreign key to expiring details;
it preserves duplicate prevention after cleanup. Database size therefore has a
small, ongoing component proportional to the number of submissions.

## API behavior and limits

Automatic zKillboard presence checks are limited to **20 killmails per five
minutes across the installation**, staggered ten seconds apart. All outbound
zKillboard requests (automatic checks, manual checks, and posts) share a
two-second request gap and an outage/rate-limit backoff. HTTP rate-limit
responses honor `Retry-After`; failures pause new requests rather than triggering
an immediate retry loop. AA's shared Redis cache is required for coordination.

Valid zKillboard lookup results are cached for **one hour** across users. Mails
younger than five minutes are skipped because zKillboard withholds them from its
query API. A positive match writes the permanent submission ledger, displays
**Pushed**, and disables posting. The ledger explains that it was already on
zKillboard and does not attribute an external submission to a local user.
Already confirmed mails are not checked again. Empty responses and outages
never mark a mail pushed. Large backlogs are processed over multiple cycles;
these checks are eventually consistent, not an immediate preflight before every
push. Names and statuses become visible when the list is reloaded.

Both enrichment jobs run after character imports and through the existing
five-minute periodic task, so older rows from previous module versions are
covered too.

CCP's character endpoint returns ID/hash pairs for up to 90 days. The module
keeps only seven days **from the kill time**, not seven days from import. This
also applies to initial imports and manual refreshes. Older detail records may
still be retrievable directly if their ID and hash are already known, but the
recent endpoint cannot discover a character's complete history beyond 90 days.
The character generally receives **losses and
final-blow kills**, not every assisted fleet kill. This is not a complete
personal combat history. See [CCP's killmail explanation](https://support.eveonline.com/hc/en-us/articles/11730655033884-Killmails)
and the [current ESI schema](https://esi.evetech.net/meta/openapi.json).

Polling observes a minimum five-minute interval. Each character task reads at
most five list pages per run, checks the newest page during backfill, overlaps
one continuation page, and resumes unfinished imports. A full old page ends
the scan of the newest-first recent list. Large histories, queue delays, ESI
caching, outages, and rate limits can delay import beyond five minutes.
Completed pages survive a later page failure. A database lease stops concurrent
imports of the same connected character; task time limits bound stuck workers.
ESI error-limit backoff is shared through AA's cache. Authorization failures
require reconnection and are displayed in the character panel.

Posting uses zKillboard's current
[`POST /api/killmail/add/{killID}/{hash}/`](https://zkillboard.com/api/docs/)
JSON endpoint. The module does not request delayed publication. Both new and
already-present success responses count as pushed. The HTTP request has a
5-second connect timeout and 15-second read timeout. Ensure the web server's
request timeout allows this round trip.

A unique, committed claim is created **before** sending the request. Concurrent
users and duplicate browser requests cannot create another push while it is
in progress or after it succeeds. Explicit JSON rejections with HTTP 401/422
allow a later user retry.

A timeout, interrupted request, malformed response, or ambiguous upstream error
locks the push as **Outcome unknown** rather than claiming success or sending
again. **Check status** performs a read-only lookup. A positive result marks it
pushed; a missing result keeps it locked because zKillboard may still be
processing the original request. zKillboard withholds very recent mails and
requests a one-hour query cache, so confirmation may take time. There is no
automatic retry/reset for an uncertain push. Copying a link is not evidence of
a successful manual submission and does not update the posting ledger.

The friendly warning uses a short-lived signed confirmation bound to the user,
killmail, and current alliance. All mutations require authentication, the module
permission, and CSRF protection. Character ownership changes invalidate prior
access. Hashes/tokens and full upstream error bodies are not written to logs by
this module. Avoid enabling verbose HTTP-client logging in production.

## Development and verification

```sh
python -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/pytest -q
.venv/bin/ruff check killpusher tests
.venv/bin/python -m django check --settings=tests.settings
.venv/bin/python -m django makemigrations --check --dry-run --settings=tests.settings
.venv/bin/python -m build
```

Tests use actual AA 5.4/Django/SDE models, SQLite, in-memory Redis, and mocked
ESI/zKillboard HTTP responses. They do not use real EVE credentials or publish
real killmails. AA's database driver dependencies may require system libraries
on a development machine; the local SQLite test environment can omit
`mysqlclient` without changing the production dependency declaration.

Live SSO, the production MySQL/MariaDB locking behavior, and scheduled Celery
execution still need a staging check in your AA deployment before rollout.
The shipped tests cover the global claim behavior but do not replace a
multi-worker production-database concurrency test.
