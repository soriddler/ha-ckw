"""Keycloak login (password + SMS code, PKCE) and refresh-token handling."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import time
from collections.abc import Awaitable, Callable
from urllib.parse import parse_qs, urlencode, urlsplit

import aiohttp

from . import kc
from .const import AUTH_URL, CLIENT_ID, REDIRECT_URI, REQUEST_TIMEOUT, SCOPE, TOKEN_URL, USER_AGENT
from .exceptions import CannotConnect, InvalidAuth, InvalidCode, LoginFlowError, ReauthRequired
from .models import Tokens

_TIMEOUT = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)


async def _request(session: aiohttp.ClientSession, method: str, url: str, **kwargs):
    """Return (status, headers, text). Never follows redirects; maps network errors."""
    headers = {"User-Agent": USER_AGENT, **kwargs.pop("headers", {})}
    try:
        async with session.request(
            method, url, headers=headers, allow_redirects=False, timeout=_TIMEOUT, **kwargs
        ) as resp:
            return resp.status, resp.headers, await resp.text()
    except (aiohttp.ClientError, asyncio.TimeoutError) as err:
        raise CannotConnect(f"{method} {urlsplit(url).netloc}: {err}") from err


def _tokens(payload: dict) -> Tokens:
    try:
        return Tokens(
            access_token=payload["access_token"],
            refresh_token=payload["refresh_token"],
            expires_at=time.time() + float(payload["expires_in"]),
        )
    except (KeyError, TypeError, ValueError) as err:
        raise LoginFlowError("token response has an unexpected structure") from err


def refresh_token_expiry(token: str) -> float | None:
    """Fixed deadline (epoch seconds) of a refresh token, read from its JWT 'exp' claim; None if it has none."""
    import json

    try:
        part = token.split(".")[1]
        exp = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))).get("exp")
        return float(exp) if exp else None
    except (IndexError, ValueError, TypeError):
        return None


def _json(text: str) -> dict:
    import json

    try:
        data = json.loads(text)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def refresh_tokens(session: aiohttp.ClientSession, refresh_token: str) -> Tokens:
    """Exchange a refresh token for new tokens. The returned refresh token REPLACES the old one."""
    status, _, text = await _request(
        session,
        "POST",
        TOKEN_URL,
        data={"grant_type": "refresh_token", "client_id": CLIENT_ID, "refresh_token": refresh_token},
    )
    payload = _json(text)
    if status == 200:
        return _tokens(payload)
    if status in (400, 401) and payload.get("error") in ("invalid_grant", "invalid_token"):
        raise ReauthRequired(payload.get("error_description") or "refresh token no longer valid")
    if status >= 500 or status == 429:
        raise CannotConnect(f"token endpoint answered HTTP {status}")
    raise LoginFlowError(f"token endpoint answered HTTP {status}: {payload.get('error')}")


class CkwLogin:
    """One interactive login: ``await start(user, pw)``, then ``await submit_code(code)``.

    ``session`` must have its own cookie jar (e.g. ``aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar())``
    or HA's ``async_create_clientsession``) because Keycloak tracks the login in cookies.
    """

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._verifier = secrets.token_urlsafe(64)
        self._action: str | None = None  # current form URL (changes after every response)

    @property
    def awaiting_code(self) -> bool:
        return self._action is not None

    async def start(self, username: str, password: str) -> None:
        """Open the login page and submit the credentials; the server then sends an SMS."""
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self._verifier.encode()).digest()).rstrip(b"=").decode()
        query = urlencode(
            {
                "client_id": CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
                "response_type": "code",
                "response_mode": "fragment",
                "scope": SCOPE,
                "state": secrets.token_urlsafe(16),
                "nonce": secrets.token_urlsafe(16),
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        status, _, html = await _request(self._session, "GET", f"{AUTH_URL}?{query}")
        action = kc.login_action(html)
        if status != 200 or kc.page_id(html) != "login" or not action:
            raise LoginFlowError(f"login page not as expected (HTTP {status}, page {kc.page_id(html)})")

        status, _, html = await _request(
            self._session, "POST", action, data={"username": username, "password": password, "credentialId": ""}
        )
        self._handle_password_response(status, html)

    def _handle_password_response(self, status: int, html: str) -> None:
        page, action = kc.page_id(html), kc.login_action(html)
        if status == 200 and page == "login-sms" and action:
            self._action = action
        elif status == 200 and page == "login":
            msg = kc.message(html)
            raise InvalidAuth(msg[1] if msg else "invalid username or password")
        else:
            raise LoginFlowError(f"unexpected answer to the password step (HTTP {status}, page {page})")

    async def resend_code(self) -> None:
        """'Send new code'. Invalidates every code sent before. Only call on explicit user request."""
        if not self._action:
            raise LoginFlowError("no login in progress")
        status, _, html = await _request(self._session, "GET", kc.without_session_code(self._action))
        action = kc.login_action(html)
        if status != 200 or kc.page_id(html) != "login-sms" or not action:
            self._action = None
            raise LoginFlowError(f"resend did not return the code page (HTTP {status})")
        self._action = action

    async def submit_code(self, code: str) -> Tokens:
        """Submit the SMS code. Raises InvalidCode (the login stays usable) or returns the tokens."""
        if not self._action:
            raise LoginFlowError("no login in progress")
        status, headers, html = await _request(self._session, "POST", self._action, data={"code": code.strip()})
        if status == 302:
            fragment = parse_qs(urlsplit(headers.get("Location", "")).fragment)
            if "code" not in fragment:
                raise LoginFlowError("redirect after the code step carries no authorization code")
            self._action = None
            return await self._exchange(fragment["code"][0])
        action = kc.login_action(html)
        if status == 200 and kc.page_id(html) == "login-sms" and action:
            self._action = action  # fresh session_code for the retry
            msg = kc.message(html)
            raise InvalidCode(msg[1] if msg else "invalid code")
        raise LoginFlowError(f"unexpected answer to the code step (HTTP {status}, page {kc.page_id(html)})")

    async def _exchange(self, auth_code: str) -> Tokens:
        status, _, text = await _request(
            self._session,
            "POST",
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "redirect_uri": REDIRECT_URI,
                "code": auth_code,
                "code_verifier": self._verifier,
            },
        )
        if status != 200:
            raise LoginFlowError(f"token exchange failed (HTTP {status})")
        return _tokens(_json(text))


class TokenManager:
    """Hands out valid access tokens and keeps the (rotating) refresh token persisted.

    ``on_refresh_token`` is awaited with every NEW refresh token BEFORE the new access token is used,
    so a crash can never lose the only valid token. Refreshes are serialized with a lock because a
    refresh token can be used only once.
    """

    def __init__(
        self,
        session: aiohttp.ClientSession,
        refresh_token: str,
        on_refresh_token: Callable[[str], Awaitable[None]],
        tokens: Tokens | None = None,
        last_refresh: float | None = None,
    ) -> None:
        self._session = session
        self._refresh_token = refresh_token
        self._on_refresh_token = on_refresh_token
        self._tokens = tokens
        self.last_refresh = last_refresh  # epoch seconds of the last successful refresh (for diagnostics)
        self._lock = asyncio.Lock()

    @property
    def session_expires_at(self) -> float | None:
        """When the login ends for good (a new SMS login is needed); None if unknown."""
        return refresh_token_expiry(self._refresh_token)

    async def access_token(self, *, force_refresh: bool = False) -> str:
        async with self._lock:
            if force_refresh or self._tokens is None or self._tokens.expires_at - time.time() < 60:
                try:
                    new = await refresh_tokens(self._session, self._refresh_token)
                except ReauthRequired as err:
                    idle = (
                        f"{(time.time() - self.last_refresh) / 3600:.1f} h"
                        if self.last_refresh
                        else "unknown time"
                    )
                    raise ReauthRequired(f"{err} (idle for {idle} since the last successful refresh)") from err
                self.last_refresh = time.time()
                if new.refresh_token != self._refresh_token:
                    await self._on_refresh_token(new.refresh_token)
                    self._refresh_token = new.refresh_token
                self._tokens = new
            return self._tokens.access_token
