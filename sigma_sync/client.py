"""Authenticated, GET-only SiGMA requests with bounded retries and polite pacing."""

from http.cookies import SimpleCookie
import random
import time
from urllib.parse import unquote, urlparse

import requests

import config
from .profiles import parse_profile

BASE_URL = "https://match.sigma.world"


class CookieExpired(RuntimeError):
    """The browser session must be replaced before any more work is done."""


class SigmaClient:
    def __init__(self, cookie: str):
        if not cookie or "\n" in cookie or "\r" in cookie:
            raise RuntimeError("Set SIGMA_COOKIE to the full browser Cookie header.")
        self.session = requests.Session()
        self.session.headers.update({
            "Cookie": cookie,
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{BASE_URL}/attendees",
        })
        self._last_request = False

    def close(self):
        self.session.close()

    def _xsrf_header(self):
        # Prefer a token rotated by the server over the initial browser token.
        token = next(
            (c.value for c in self.session.cookies if c.name == "XSRF-TOKEN"), None
        )
        if not token:
            cookies = SimpleCookie()
            cookies.load(self.session.headers["Cookie"])
            if "XSRF-TOKEN" in cookies:
                token = cookies["XSRF-TOKEN"].value
        return {"X-XSRF-TOKEN": unquote(token)} if token else {}

    def get(self, path: str, *, accept="application/json"):
        """Never follow redirects or send a Cookie header to another origin."""
        if not path.startswith("/attendees") or path.startswith("//"):
            raise ValueError("SiGMA client only permits attendee endpoints.")
        for attempt in range(config.SWAPCARD_MAX_RETRIES + 1):
            if self._last_request:
                time.sleep(random.uniform(config.PAGE_DELAY_MIN, config.PAGE_DELAY_MAX))
            self._last_request = True
            try:
                response = self.session.get(
                    BASE_URL + path,
                    headers={"Accept": accept, **self._xsrf_header()},
                    timeout=config.REQUEST_TIMEOUT,
                    allow_redirects=False,
                )
            except (requests.Timeout, requests.ConnectionError):
                if attempt == config.SWAPCARD_MAX_RETRIES:
                    raise RuntimeError("SiGMA request failed after network retries.") from None
                time.sleep(config.SWAPCARD_RETRY_WAIT * 2 ** attempt)
                continue
            location = urlparse(response.headers.get("Location", "")).path
            if response.status_code in (401, 419) or (
                response.is_redirect and location.rstrip("/").endswith("/login")
            ):
                raise CookieExpired("SiGMA cookie expired, paste a new one into SIGMA_COOKIE.")
            if response.status_code == 429 or response.status_code >= 500:
                if attempt < config.SWAPCARD_MAX_RETRIES:
                    try:
                        wait = max(0, float(response.headers["Retry-After"]))
                    except (KeyError, ValueError):
                        wait = config.SWAPCARD_RETRY_WAIT * 2 ** attempt
                    print(f"  [sigma] HTTP {response.status_code}; retrying in {wait:.1f}s")
                    time.sleep(wait)
                    continue
            if response.is_redirect:
                raise RuntimeError("Unexpected SiGMA redirect; check your browser session.")
            if response.status_code != 200:
                raise RuntimeError(
                    f"SiGMA returned HTTP {response.status_code}; check session/access. "
                    "No attendee was imported from this response."
                )
            if "text/html" in response.headers.get("Content-Type", ""):
                # Some session middleware returns the login form with HTTP 200.
                if 'type="password"' in response.text or "name=\"password\"" in response.text:
                    raise CookieExpired("SiGMA cookie expired, paste a new one into SIGMA_COOKIE.")
            return response
        raise RuntimeError("SiGMA retries exhausted.")

    def attendees(self, page: int) -> tuple[list, int]:
        response = self.get(f"/attendees?page={page}")
        try:
            data = response.json()
            attendees = data["attendees"]
            total = int(data["totalUserCount"])
        except (ValueError, KeyError, TypeError):
            raise RuntimeError("Unexpected SiGMA list response; expected attendees JSON.") from None
        if not isinstance(attendees, list) or total < 0:
            raise RuntimeError("Invalid SiGMA attendees list or total.")
        return attendees, total

    def profile(self, attendee_id: str) -> dict:
        response = self.get(f"/attendees/{attendee_id}")
        # A content-negotiation response can be JSON, Inertia HTML with embedded
        # JSON, or conventional server-rendered HTML. Parse only the attendee.
        return parse_profile(response.text)
