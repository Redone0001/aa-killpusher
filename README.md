# AA Killmail Pusher

An Alliance Auth **5.4** module for deliberately publishing character killmails
to zKillboard. Based on the integration pattern in
[allianceauth_example](https://github.com/ErikKalkoken/allianceauth_example),
with modern AA/Django hooks and templates.

## What users get

- Connect any character already owned by their AA account using EVE SSO.
- List the last **7 days** of imported kills and losses, with a kill/loss filter
  and 50 character entries per page.
- Push a killmail without navigating away from the page.
- Confirm before publishing a victim from the **current AA main character's
  alliance**. This applies to matching losses too. Two unaffiliated characters
  are not treated as alliance mates. AA's stored main-character affiliation is
  used at click time; its freshness follows AA's character updates.
- See a persistent **Pushed** state and no repeat push after success, shared
  across every user who has access to that killmail.
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
git checkout v0.1.0
python -m pip install .
```

GitHub authentication is needed for a private repository. Do not place access
tokens in install URLs, shell history, or settings files. If the repository is
public, you can instead install the tagged version directly:

```sh
python -m pip install 'git+https://github.com/Redone0001/aa-killpusher.git@v0.1.0'
```

Alternatively, install a built wheel:

```sh
pip install /path/to/aa_killpusher-0.1.0-py3-none-any.whl
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

# Replace this with your actual Auth URL and maintainer contact.
KILLPUSHER_USER_AGENT = "My Alliance Auth / https://auth.example.org / admin@example.org"

CELERYBEAT_SCHEDULE["Killmail Pusher :: Import and cleanup"] = {
    "task": "killpusher.tasks.poll_killmails",
    "schedule": crontab(minute="*/5"),
}
```

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

Connect a character, allow a scheduled import to finish, and review its import
status in **Connected characters and import status**. For initial diagnosis,
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
| Character association and kill/loss role | With the killmail, or until disconnect |
| Tracking, authorization reference, poll progress and safe error message | While connected |
| Submission ID, state, user reference, timestamps and brief result | Successful/uncertain records permanently; rejected records 7 days from attempt |

Full attacker lists, damage, fitting, cargo, coordinates, and raw killmail JSON
are **not stored**. Ship and system names are looked up in your existing SDE.
Victim pilot/corporation names use existing AA records, with an ID fallback for
unknown entities. This avoids another entity/name cache in the module.

Cleanup runs with every five-minute import task. Old details are also excluded
from the page and push endpoint immediately, even if a cleanup run is delayed.
Import ignores mails older than seven days, so expired details stay expired.
The compact permanent posting ledger has no foreign key to expiring details;
it preserves duplicate prevention after cleanup. Database size therefore has a
small, ongoing component proportional to the number of submissions.

## API behavior and limits

CCP's character endpoint returns ID/hash pairs for up to 90 days. The module
keeps only seven days. The character generally receives **losses and
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
