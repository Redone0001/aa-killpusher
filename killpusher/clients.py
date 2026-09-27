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
    def __init__(self, token=None):
        self.token = token

    def get(self, path, *, authenticated=False, params=None):
        return self.request("GET", path, authenticated=authenticated, params=params)

    def request(self, method, path, *, authenticated=False, params=None, json=None):
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
            response = requests.request(
                method,
                f"{ESI}{path}",
                params=params,
                json=json,
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

    def names(self, ids):
        data, _ = self.request("POST", "/universe/names", json=sorted(set(ids)))
        if not isinstance(data, list):
            raise RemoteError("CCP returned an unexpected name list.")
        return data


@dataclass(frozen=True)
class PostResult:
    state: str
    message: str


def post_killmail(killmail):
    if not reserve_zkill_request():
        return PostResult("rejected", "zKillboard requests are paused briefly. Please retry later.")
    # No automatic HTTP retries: a timeout can happen after zKillboard accepts the POST.
    try:
        response = requests.post(
            f"{ZKILL}/api/killmail/add/{killmail.pk}/{killmail.hash}/",
            headers=headers(),
            timeout=TIMEOUT,
            allow_redirects=False,
        )
    except requests.RequestException:
        pause_zkill(60)
        return PostResult("unknown", "No reliable response. Push locked to prevent a duplicate.")
    if response.status_code in (420, 429):
        pause_zkill(retry_seconds(response.headers.get("Retry-After")))
    elif response.status_code >= 500:
        pause_zkill(60)
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
    """True = found, False = absent from a valid response, None = not established."""
    key = f"killpusher:public:{killmail_id}"
    cached = cache.get(key)
    if cached is not None:
        return cached
    if not reserve_zkill_request():
        return None
    try:
        response = requests.get(
            f"{ZKILL}/api/killID/{killmail_id}/",
            headers=headers(),
            timeout=TIMEOUT,
            allow_redirects=False,
        )
    except requests.RequestException:
        pause_zkill(60)
        return None
    if response.status_code != 200:
        pause_zkill(retry_seconds(response.headers.get("Retry-After"), default=300))
        return None
    try:
        data = response.json()
    except ValueError:
        pause_zkill(60)
        return None
    if not isinstance(data, list) or any(
        not isinstance(row, dict) or not isinstance(row.get("killmail_id"), int) for row in data
    ):
        pause_zkill(60)
        return None
    found = any(row["killmail_id"] == killmail_id for row in data)
    # zKillboard requests a one-hour client cache for killmail queries.
    cache.set(key, found, timeout=3600)
    return found


def reserve_zkill_request():
    """Shared across background reads, manual checks, and posting workers."""
    if cache.get("killpusher:zkill:backoff"):
        return False
    return cache.add("killpusher:zkill:request", True, timeout=2)


def pause_zkill(seconds):
    until = timezone.now() + timedelta(seconds=max(1, seconds))
    existing = cache.get("killpusher:zkill:backoff")
    if existing and existing >= until:
        return
    cache.set("killpusher:zkill:backoff", until, timeout=max(1, seconds))
