#!/usr/bin/env python3
"""Probe the CKW (Keycloak) login flow and report how it behaves.

Run it yourself in a normal terminal (it asks for your password and the SMS code):

    python3 -I tools/probe_login.py --username you@example.ch [--wrong-code] [--save-state state.json]
    python3 -I tools/probe_login.py --refresh state.json          # later, to measure token lifetime

The report is safe to paste: it prints status codes, page ids, error summaries,
lifetimes and booleans. It NEVER prints passwords, SMS codes, tokens or cookies.

Options
  --wrong-code    send ONE wrong SMS code (000000) first and report the reaction.
                  Careful: too many wrong attempts could lock the account. Only 1 is sent.
  --resend        after the password step, request a NEW SMS (like the browser button) and test
                  whether the first code still works. Needs both SMS codes, enter them when asked.
  --offline       request the offline_access scope at login. If allowed, the refresh token is of
                  type 'Offline' and may not have the 30-day hard cap. Use a separate --save-state file.
  --save-state P  store the refresh token in P (chmod 600) for the --refresh runs.
  --refresh P     no login; refresh using the token in P, update P, print lifetimes.
  --test-reuse    after refreshing, try the OLD refresh token again (tells whether
                  rotation/reuse detection is on). May kill the session. Not with --save-state.
Stdlib only.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import http.cookiejar
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

IDP = "https://id.ckw.ch/realms/CKW-prod/protocol/openid-connect"
CLIENT_ID = "dtp-web"
REDIRECT = "https://my.ckw.ch/stromverbrauch"
API = "https://dtp-apim.azure-api.net"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0 Safari/537.36"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), NoRedirect)


def http(method, url, data=None, headers=None):
    """Return (status, headers, body_text). Never follows redirects."""
    h = {"User-Agent": UA, "Accept-Encoding": "identity", **(headers or {})}
    if isinstance(data, dict):
        data = urllib.parse.urlencode(data).encode()
        h["Content-Type"] = "application/x-www-form-urlencoded"
    elif isinstance(data, str):
        data = data.encode()
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with opener.open(req, timeout=30) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read().decode("utf-8", "replace")


# ---- kcContext helpers ---------------------------------------------------
def page_id(html: str) -> str | None:
    m = re.search(r'"pageId"\s*:\s*"([^"]+)"', html)
    return m.group(1) if m else None


def login_action(html: str) -> str | None:
    m = re.search(r'"loginAction"\s*:\s*"([^"]+)"', html)
    return json.loads(f'"{m.group(1)}"') if m else None


def message(html: str) -> str | None:
    m = re.search(r'"message"\s*:\s*\{\s*"summary"\s*:\s*"([^"]*)"\s*,\s*"type"\s*:\s*"(\w+)"', html)
    return f"{m.group(2)}: {m.group(1)}" if m else None


def describe(label: str, status: int, hdrs: dict, body: str) -> None:
    print(f"[{label}] HTTP {status}  pageId={page_id(body)}  message={message(body)}"
          + (f"  redirect->{urllib.parse.urlsplit(hdrs.get('location', '')).netloc}" if status in (301, 302, 303) else ""))


# ---- JWT / token helpers -------------------------------------------------
def jwt_claims(tok: str) -> dict:
    p = tok.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))


def fp(tok: str) -> str:  # short fingerprint, enough to compare tokens, reveals nothing
    return hashlib.sha256(tok.encode()).hexdigest()[:8]


def report_tokens(label: str, status: int, body: str, old_refresh: str | None = None) -> dict | None:
    print(f"[{label}] HTTP {status}")
    try:
        t = json.loads(body)
    except ValueError:
        print("   non-JSON response")
        return None
    if status != 200:
        print("   error:", t.get("error"), "-", t.get("error_description"))
        return None
    ac = jwt_claims(t["access_token"])
    print(f"   expires_in={t.get('expires_in')}s  refresh_expires_in={t.get('refresh_expires_in')}s "
          f"(~{t.get('refresh_expires_in', 0) / 86400:.2f} days; 0 = no fixed expiry)  scope={t.get('scope')!r}")
    print(f"   access token claims: iss ok={ac.get('iss', '').endswith('/CKW-prod')}  "
          f"lifetime={ac.get('exp', 0) - ac.get('iat', 0)}s  azp={ac.get('azp')}  has_sid={'sid' in ac}")
    rc = jwt_claims(t["refresh_token"]) if t["refresh_token"].count(".") == 2 else {}
    if rc:
        exp = rc.get("exp")
        print(f"   refresh token: typ={rc.get('typ')}  "
              + (f"exp-iat={exp - rc.get('iat', 0)}s  exp={time.strftime('%Y-%m-%d %H:%M', time.localtime(exp))}"
                 if exp else "NO exp claim (no fixed expiry)"))
    if old_refresh is not None:
        print(f"   refresh token ROTATED (new != old): {fp(t['refresh_token']) != fp(old_refresh)}")
    return t


def token_request(data: dict):
    return http("POST", f"{IDP}/token", {"client_id": CLIENT_ID, **data})


def api_check(access: str) -> None:
    s, _, b = http("GET", f"{API}/account-api/api/v1/account", headers={"Authorization": f"Bearer {access}",
                                                                         "Origin": "https://my.ckw.ch"})
    n = None
    try:
        n = len(json.loads(b)["body"]["accounts"])
    except Exception:
        pass
    print(f"[api account] HTTP {s}  accounts={n}")


def save_state(path: str, refresh: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"refresh_token": refresh, "saved": int(time.time())}, f)
    print(f"   state saved to {path} (chmod 600)")


# ---- modes ---------------------------------------------------------------
def refresh_mode(path: str, test_reuse: bool) -> int:
    st = json.load(open(path))
    old = st["refresh_token"]
    print(f"state age: {(time.time() - st['saved']) / 3600:.1f} h")
    s, _, b = token_request({"grant_type": "refresh_token", "refresh_token": old})
    t = report_tokens("refresh", s, b, old)
    if not t:
        return 1
    try:
        po, pn = jwt_claims(old)["exp"], jwt_claims(t["refresh_token"])["exp"]
        if not po or not pn:
            raise KeyError
        print(f"   refresh-token expiry vs previous token: {pn - po:+d}s "
              f"(0 = absolute cap fixed at login; ~ time since last refresh = sliding/renewed)")
    except Exception:
        print("   (refresh tokens are not JWTs, cannot compare expiry)")
    exp = jwt_claims(t["refresh_token"]).get("exp") if t["refresh_token"].count(".") == 2 else None
    print(f"   exact expiry: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(exp)) if exp else 'none (no exp claim)'}"
          f"   seconds since last run: {time.time() - st['saved']:.0f}")
    save_state(path, t["refresh_token"])
    api_check(t["access_token"])
    if test_reuse:
        s2, _, b2 = token_request({"grant_type": "refresh_token", "refresh_token": old})
        print(f"[reuse of OLD refresh token] HTTP {s2}  ->", "accepted" if s2 == 200 else "rejected")
    return 0


def login_mode(a) -> int:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    q = urllib.parse.urlencode({
        "client_id": CLIENT_ID, "redirect_uri": REDIRECT, "response_type": "code", "response_mode": "fragment",
        "scope": "openid offline_access" if a.offline else "openid", "state": secrets.token_urlsafe(16), "nonce": secrets.token_urlsafe(16),
        "code_challenge": challenge, "code_challenge_method": "S256"})
    s, h, body = http("GET", f"{IDP}/auth?{q}")
    describe("1 open login page", s, h, body)
    action = login_action(body)
    if s != 200 or not action:
        print("cannot continue (blocked by Cloudflare/bot protection? no loginAction)")
        return 1
    print("   cookies after step 1:", sorted(c.name for c in jar))

    pw = getpass.getpass("CKW password (hidden): ")
    s, h, body = http("POST", action, {"username": a.username, "password": pw, "credentialId": ""})
    describe("2 submit password", s, h, body)
    action = login_action(body)
    if page_id(body) != "login-sms" or not action:
        print("password step failed -> stop (not retrying, avoids lockout)")
        return 1

    if a.wrong_code:
        t0 = time.time()
        s, h, body2 = http("POST", action, {"code": "000000"})
        describe("3a WRONG sms code", s, h, body2)
        print(f"   body contains 'Invalid SMS code entered': {'Invalid SMS code entered' in body2}")
        print(f"   took {time.time() - t0:.1f}s; new loginAction present={bool(login_action(body2))}; "
              f"same page as before={page_id(body2) == 'login-sms'}")
        action = login_action(body2) or action
        if page_id(body2) != "login-sms":
            print("   flow left the SMS page after a wrong code -> need to restart login (a new SMS may be sent)")
            return 1
        print("   (was a NEW SMS sent for the wrong-code attempt? check your phone, then enter the ORIGINAL code)")

    res = None
    if a.resend:
        # "send new code" in the browser = plain GET of the SMS step URL without session_code (see HAR)
        u = urllib.parse.urlsplit(action)
        qs = [(k, v) for k, v in urllib.parse.parse_qsl(u.query) if k != "session_code"]
        s, h, body3 = http("GET", urllib.parse.urlunsplit(u._replace(query=urllib.parse.urlencode(qs))))
        describe("3b request NEW code", s, h, body3)
        if page_id(body3) != "login-sms" or not login_action(body3):
            print("   resend did not return the SMS page -> stop")
            return 1
        action = login_action(body3)
        print("   A NEW SMS should be arriving now. Did it? (note it for the report)")
        old = input("OLD (first) SMS code, to test whether it is still valid after the resend (Enter = skip): ").strip()
        if old:
            s, h, b4 = http("POST", action, {"code": old})
            describe("3c OLD code after resend", s, h, b4)
            if s == 302:
                print("   -> OLD code still ACCEPTED after resend")
                res = (s, h, b4)
            else:
                print("   -> OLD code rejected after resend (only the newest code counts)")
                action = login_action(b4) or action
    if res is None:
        code = input("SMS code (digits only; the NEWEST one if you resent): ").strip()
        res = http("POST", action, {"code": code})
    s, h, body = res
    describe("4 submit sms code", s, h, body)
    loc = h.get("location", "")
    frag = urllib.parse.parse_qs(urllib.parse.urlsplit(loc).fragment)
    if s != 302 or "code" not in frag:
        print(f"   sms code NOT accepted (status {s}, location present={bool(loc)}, fragment keys={sorted(frag)})")
        return 1

    s, _, b = token_request({"grant_type": "authorization_code", "code": frag["code"][0],
                             "redirect_uri": REDIRECT, "code_verifier": verifier})
    t = report_tokens("5 token exchange", s, b)
    if not t:
        return 1
    if a.save_state:
        save_state(a.save_state, t["refresh_token"])  # save first, in case a later step fails
    api_check(t["access_token"])

    s, _, b = token_request({"grant_type": "refresh_token", "refresh_token": t["refresh_token"]})
    t2 = report_tokens("6 immediate refresh", s, b, t["refresh_token"])
    if not t2:
        return 1
    if a.test_reuse and not a.save_state:
        s, _, _ = token_request({"grant_type": "refresh_token", "refresh_token": t["refresh_token"]})
        print(f"[7 reuse of OLD refresh token] HTTP {s} ->", "accepted" if s == 200 else "rejected")
    if a.save_state:
        save_state(a.save_state, t2["refresh_token"])
        print("   now run daily:  python3 -I tools/probe_login.py --refresh", a.save_state)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--username")
    ap.add_argument("--wrong-code", action="store_true")
    ap.add_argument("--resend", action="store_true",
                    help="also test 'send new code' (GET of the SMS step) and whether the OLD code still works")
    ap.add_argument("--offline", action="store_true",
                    help="request scope 'openid offline_access' (long-lived offline refresh token) at login")
    ap.add_argument("--save-state")
    ap.add_argument("--refresh")
    ap.add_argument("--test-reuse", action="store_true")
    a = ap.parse_args()
    if a.refresh:
        return refresh_mode(a.refresh, a.test_reuse)
    if not a.username:
        ap.error("--username is required for a login run")
    return login_mode(a)


if __name__ == "__main__":
    sys.exit(main())
