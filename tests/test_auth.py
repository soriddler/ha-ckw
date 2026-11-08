import asyncio
import re
import time
from urllib.parse import parse_qs, urlsplit

import aiohttp
import pytest
from aioresponses import aioresponses

from ckw_client import (
    CannotConnect,
    CkwLogin,
    InvalidAuth,
    InvalidCode,
    LoginFlowError,
    ReauthRequired,
    TokenManager,
    refresh_tokens,
)
from ckw_client.const import AUTH_URL, TOKEN_URL

AUTH_RE = re.compile(r"^" + re.escape(AUTH_URL) + r"\?.*")
ACTION_RE = re.compile(r"^https://id\.ckw\.ch/realms/CKW-prod/login-actions/authenticate.*")
REDIRECT = "https://my.ckw.ch/stromverbrauch#state=s&session_state=x&iss=i&code=AUTHCODE"
TOKEN_JSON = {"access_token": "AT1", "refresh_token": "RT1", "expires_in": 300, "refresh_expires_in": 0}


@pytest.fixture
async def session():
    async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar()) as s:
        yield s


def calls(m, method, pattern):
    return [c for (meth, url), cs in m.requests.items() if meth == method and pattern.match(str(url)) for c in cs]


async def test_full_login_happy_path(session, fx):
    with aioresponses() as m:
        m.get(AUTH_RE, body=fx("kc_wrong_password.js"))  # any page with pageId=login + loginAction
        m.post(ACTION_RE, body=fx("kc_after_password_sms_page.js"))
        m.post(ACTION_RE, status=302, headers={"Location": REDIRECT})
        m.post(TOKEN_URL, payload=TOKEN_JSON)

        login = CkwLogin(session)
        await login.start("user@example.org", "pw")
        assert login.awaiting_code
        tokens = await login.submit_code(" 123456 ")

        assert (tokens.access_token, tokens.refresh_token) == ("AT1", "RT1")
        assert tokens.expires_at > time.time() + 200
        assert not login.awaiting_code

        auth_url = next(u for (me, u) in m.requests if me == "GET")
        q = parse_qs(urlsplit(str(auth_url)).query)
        assert q["scope"] == ["openid offline_access"] and q["code_challenge_method"] == ["S256"]
        post = calls(m, "POST", ACTION_RE)
        assert post[0].kwargs["data"] == {"username": "user@example.org", "password": "pw", "credentialId": ""}
        assert post[1].kwargs["data"] == {"code": "123456"}  # whitespace stripped
        tok = calls(m, "POST", re.compile(re.escape(TOKEN_URL)))[0].kwargs["data"]
        assert tok["grant_type"] == "authorization_code" and tok["code"] == "AUTHCODE" and tok["code_verifier"]


async def test_wrong_password(session, fx):
    with aioresponses() as m:
        m.get(AUTH_RE, body=fx("kc_wrong_password.js"))
        m.post(ACTION_RE, body=fx("kc_wrong_password.js"))
        with pytest.raises(InvalidAuth):
            await CkwLogin(session).start("u", "bad")


async def test_wrong_code_keeps_session_and_allows_retry(session, fx):
    with aioresponses() as m:
        m.get(AUTH_RE, body=fx("kc_wrong_password.js"))
        m.post(ACTION_RE, body=fx("kc_after_password_sms_page.js"))
        m.post(ACTION_RE, body=fx("kc_wrong_sms_code.js"))
        m.post(ACTION_RE, status=302, headers={"Location": REDIRECT})
        m.post(TOKEN_URL, payload=TOKEN_JSON)
        login = CkwLogin(session)
        await login.start("u", "pw")
        with pytest.raises(InvalidCode):
            await login.submit_code("000000")
        assert login.awaiting_code
        assert (await login.submit_code("123456")).refresh_token == "RT1"


async def test_resend_uses_url_without_session_code(session, fx):
    with aioresponses() as m:
        m.get(AUTH_RE, body=fx("kc_wrong_password.js"))
        m.post(ACTION_RE, body=fx("kc_after_password_sms_page.js"))
        m.get(ACTION_RE, body=fx("kc_after_password_sms_page.js"))
        login = CkwLogin(session)
        await login.start("u", "pw")
        await login.resend_code()
        assert login.awaiting_code
        url = [u for (me, u) in m.requests if me == "GET" and "login-actions" in str(u)][0]
        assert "session_code" not in str(url) and "execution=" in str(url)


async def test_unexpected_pages_raise_flow_error(session):
    with aioresponses() as m:
        m.get(AUTH_RE, status=403, body="<html>Cloudflare</html>")
        with pytest.raises(LoginFlowError):
            await CkwLogin(session).start("u", "pw")


async def test_submit_without_start(session):
    with pytest.raises(LoginFlowError):
        await CkwLogin(session).submit_code("1")


async def test_network_error_is_cannot_connect(session):
    with aioresponses() as m:
        m.get(AUTH_RE, exception=aiohttp.ClientConnectionError("down"))
        with pytest.raises(CannotConnect):
            await CkwLogin(session).start("u", "pw")


# ------------------------------------------------------------ refresh ----------
async def test_refresh_ok_invalid_grant_and_server_error(session):
    with aioresponses() as m:
        m.post(TOKEN_URL, payload=TOKEN_JSON)
        assert (await refresh_tokens(session, "old")).refresh_token == "RT1"
        m.post(TOKEN_URL, status=400, payload={"error": "invalid_grant", "error_description": "Token is not active"})
        with pytest.raises(ReauthRequired):
            await refresh_tokens(session, "old")
        m.post(TOKEN_URL, status=502, body="bad gateway")
        with pytest.raises(CannotConnect):
            await refresh_tokens(session, "old")


async def test_token_manager_persists_rotated_token_before_use(session):
    saved = []
    order = []

    async def persist(tok):
        saved.append(tok)
        order.append("persist")

    with aioresponses() as m:
        m.post(TOKEN_URL, payload={**TOKEN_JSON, "access_token": "AT-A", "refresh_token": "RT-B"})
        tm = TokenManager(session, "RT-A", persist)
        assert await tm.access_token() == "AT-A"
        assert saved == ["RT-B"]
        assert await tm.access_token() == "AT-A"  # still valid -> no second refresh
        assert len(calls(m, "POST", re.compile(re.escape(TOKEN_URL)))) == 1

        m.post(TOKEN_URL, payload={**TOKEN_JSON, "access_token": "AT-C", "refresh_token": "RT-C"})
        assert await tm.access_token(force_refresh=True) == "AT-C"
        second = calls(m, "POST", re.compile(re.escape(TOKEN_URL)))[1]
        assert second.kwargs["data"]["refresh_token"] == "RT-B"  # uses the rotated token
        assert saved == ["RT-B", "RT-C"]


async def test_token_manager_serializes_concurrent_refreshes(session):
    async def persist(tok):
        await asyncio.sleep(0)

    with aioresponses() as m:
        m.post(TOKEN_URL, payload=TOKEN_JSON)
        tm = TokenManager(session, "RT-A", persist)
        results = await asyncio.gather(*(tm.access_token() for _ in range(5)))
        assert results == ["AT1"] * 5
        assert len(calls(m, "POST", re.compile(re.escape(TOKEN_URL)))) == 1
