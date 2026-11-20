"""Client for the Urja portal's undocumented endpoints (see PROTOCOL.md)."""
import hashlib
import hmac
import logging
import random
import time
from collections import deque

import httpx

from .config import Settings

log = logging.getLogger(__name__)
SESSION_TTL_S = 3600  # portal cookie Max-Age
REFRESH_MARGIN_S = 300


class PortalError(Exception):
    pass


class PortalAuthError(PortalError):
    pass


class PortalNotFound(PortalError):
    pass


def sign(secret: str, method: str, path: str, query: str, ts: str) -> str:
    msg = "\n".join([method, path, query, ts])
    return hmac.new(secret.encode(), msg.encode(), hashlib.sha256).hexdigest()


class PortalClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None, sleep=time.sleep):
        self.s = settings
        self._sleep = sleep
        self._http = httpx.Client(base_url=settings.portal_url, timeout=20, transport=transport)
        self._logged_in_at = 0.0
        self._sent: deque[float] = deque()

    def _throttle(self):
        """Portal answers 429 after ~120 requests/min (see PROTOCOL.md); stay under it proactively."""
        now = time.monotonic()
        while self._sent and now - self._sent[0] > 60:
            self._sent.popleft()
        if len(self._sent) >= self.s.portal_max_per_min:
            self._sleep(60 - (now - self._sent[0]) + 0.5)
        self._sent.append(time.monotonic())

    def close(self):
        self._http.close()

    def login(self) -> None:
        r = self._http.post(
            "/login",
            data={"email": self.s.portal_email, "password": self.s.portal_password},
            headers={"Origin": self.s.portal_url},  # SvelteKit CSRF check
        )
        if r.status_code != 200 or '"redirect"' not in r.text:
            raise PortalAuthError(f"portal login failed ({r.status_code})")
        self._logged_in_at = time.time()
        log.info("portal login ok")

    def _ensure_session(self):
        if time.time() - self._logged_in_at > SESSION_TTL_S - REFRESH_MARGIN_S:
            self.login()

    def _get(self, path: str, params: dict | None = None, headers: dict | None = None, retries: int = 4):
        """GET JSON with session refresh on 401 and backoff on 5xx/network errors."""
        self._ensure_session()
        relogged, throttled = False, 0
        for attempt in range(retries + 1):
            self._throttle()
            self._sleep(self.s.request_delay_s + random.uniform(0, 0.1))
            try:
                r = self._http.get(path, params=params, headers=headers)
            except httpx.TransportError as e:
                err = f"network error: {e}"
            else:
                if r.status_code == 401 and not relogged and "signature" not in r.text:
                    relogged = True
                    self.login()
                    continue
                if r.status_code == 429 and throttled < 5:
                    throttled += 1
                    wait = float(r.headers.get("retry-after") or 15 * throttled)
                    log.warning("portal rate-limited us, sleeping %.0fs", wait)
                    self._sleep(wait)
                    continue
                if r.status_code == 404:
                    raise PortalNotFound(path)
                if r.status_code < 400:
                    try:
                        return r.json()
                    except ValueError:
                        err = "non-JSON response"
                elif r.status_code in (401, 403):
                    raise PortalAuthError(f"{path}: {r.status_code} {r.text[:120]}")
                else:
                    err = f"HTTP {r.status_code}"
            if attempt == retries:
                raise PortalError(f"{path}: {err}")
            delay = min(30, 0.5 * 2**attempt) + random.uniform(0, 0.3)
            log.warning("portal %s failed (%s), retry in %.1fs", path, err, delay)
            self._sleep(delay)

    def dts(self) -> list[dict]:
        out, page = [], 1
        while True:
            d = self._get("/portal/dts", {"page": page})
            out += d["data"]
            if not d["data"] or len(out) >= d["total"]:
                return out
            page += 1

    def export(self) -> list[dict]:
        """Bulk export of all meters (with hierarchy + geo). Needs an HMAC-signed request.

        The `page` param is accepted but ignored by the portal (page 1 returns everything);
        we still verify the count against `total` and fall back to paging if it ever starts to honour it.
        """
        out: dict[str, dict] = {}
        page = 1
        while True:
            secret = self._get("/portal/keys")["data"]["signingSecret"]
            q, ts = f"page={page}", str(int(time.time()))
            d = self._get("/portal/export", {"page": page}, {"x-timestamp": ts, "x-signature": sign(secret, "GET", "/portal/export", q, ts)})
            before = len(out)
            for m in d["data"]:
                out[m["meterId"]] = m
            if len(out) >= d["total"] or len(out) == before:
                return list(out.values())
            page += 1

    def energy(self, meter_id: str) -> list[dict]:
        return self._get(f"/portal/meters/{meter_id}/energy")["data"]
