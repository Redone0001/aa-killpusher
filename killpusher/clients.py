"""Small HTTP boundary; never persist or log response bodies, tokens, or hashes."""

from dataclasses import dataclass
from datetime import timedelta
from email.utils import parsedate_to_datetime

import requests
from django.core.cache import cache
from django.utils import timezone

from .conf import user_agent

ESI = "https://esi.evetech.net"
ZKILL = "https://zkillboard.com"
TIMEOUT = (5, 15)


class RemoteError(Exception):
    def __init__(self, message, retry_after=300, unauthorized=False):
        super().__init__(message)
        self.retry_after = retry_after
        self.unauthorized = unauthorized


def retry_seconds(value, default=900):
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        try:
            return max(1, int((parsedate_to_datetime(value) - timezone.now()).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return default


def headers():
    return {"User-Agent": user_agent(), "Accept-Encoding": "gzip", "Accept": "application/json"}


class EsiClient:
    def __init__(self, token):
        self.token = token

    def get(self, path, *, authenticated=False, params=None):
        delay_until = cache.get("killpusher:esi:backoff")
        if delay_until and delay_until > timezone.now():
            raise RemoteError(
                "CCP rate limit; import will resume automatically.",
                int((delay_until - timezone.now()).total_seconds()) + 1,
            )
        request_headers = headers()
        request_headers["X-Compatibility-Date"] = "2026-09-27"
        if authenticated:
            request_headers["Authorization"] = f"Bearer {self.token.valid_access_token()}"
        try:
            response = requests.get(
                f"{ESI}{path}",
                params=params,
                headers=request_headers,
                timeout=TIMEOUT,
                allow_redirects=False,
            )
        except requests.RequestException:
            raise RemoteError("CCP connection failed; import will retry.") from None
        remaining = response.headers.get("X-Esi-Error-Limit-Remain")
        if response.status_code == 420 or (remaining is not None and int(remaining) < 10):
            wait = retry_seconds(response.headers.get("X-Esi-Error-Limit-Reset"))
            cache.set("killpusher:esi:backoff", timezone.now() + timedelta(seconds=wait), wait)
        if response.status_code in (420, 429):
            raise RemoteError(
                "CCP rate limit; import will resume automatically.",
                retry_seconds(response.headers.get("Retry-After")),
            )
        if response.status_code in (401, 403):
            raise RemoteError(
                "ESI authorization expired. Reconnect this character.", unauthorized=True
            )
        if response.status_code != 200:
            raise RemoteError(f"CCP returned HTTP {response.status_code}; import will retry.")
        try:
            return response.json(), response.headers
        except ValueError:
            raise RemoteError("CCP returned an unreadable response; import will retry.") from None

    def recent(self, character_id, page):
        data, response_headers = self.get(
            f"/characters/{character_id}/killmails/recent",
            authenticated=True,
            params={"page": page},
        )
        if not isinstance(data, list):
            raise RemoteError("CCP returned an unexpected killmail list.")
        return data, max(1, int(response_headers.get("X-Pages", 1)))

    def detail(self, killmail_id, killmail_hash):
        return self.get(f"/killmails/{killmail_id}/{killmail_hash}")[0]


@dataclass(frozen=True)
class PostResult:
    state: str
    message: str


def post_killmail(killmail):
    # No automatic HTTP retries: a timeout can happen after zKillboard accepts the POST.
    try:
        response = requests.post(
            f"{ZKILL}/api/killmail/add/{killmail.pk}/{killmail.hash}/",
            headers=headers(),
            timeout=TIMEOUT,
            allow_redirects=False,
        )
    except requests.RequestException:
        return PostResult("unknown", "No reliable response. Push locked to prevent a duplicate.")
    try:
        data = response.json()
    except ValueError:
        data = {}
    if isinstance(data, dict):
        if response.status_code == 200 and data.get("status") == "success":
            return PostResult("submitted", "Accepted by zKillboard.")
        if response.status_code in (401, 422) and data.get("error"):
            return PostResult(
                "rejected", "zKillboard rejected this request. You can try again later."
            )
    return PostResult("unknown", "Acceptance could not be verified. Push locked pending a check.")


def is_public(killmail_id):
    """Positive-only reconciliation. Absence is never evidence that a POST failed."""
    key = f"killpusher:public:{killmail_id}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    try:
        response = requests.get(
            f"{ZKILL}/api/killID/{killmail_id}/",
            headers=headers(),
            timeout=TIMEOUT,
            allow_redirects=False,
        )
        data = response.json()
    except (requests.RequestException, ValueError):
        return False
    found = (
        response.status_code == 200
        and isinstance(data, list)
        and any(isinstance(row, dict) and row.get("killmail_id") == killmail_id for row in data)
    )
    # zKillboard requests a one-hour client cache for killmail queries.
    cache.set(key, found, timeout=3600)
    return found
