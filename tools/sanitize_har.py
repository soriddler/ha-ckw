#!/usr/bin/env python3
"""Sanitize a browser HAR recording so it can be shared safely.

Keeps what is needed to reverse-engineer a login/download flow (URLs, methods,
parameter NAMES, header names, cookie names and lifetimes, status codes,
redirects, JSON structure, JWT claim names) and removes secrets and personal
data (passwords, TANs, tokens, cookie values, downloaded files, your name,
address, customer number, meter id, e-mail).

Usage:
    python3 -I sanitize_har.py my.har \
        --redact "Your Name" --redact "your@mail.ch" --redact 1234567 \
        --redact "Street 1" --redact "your-username"

Output: my.sanitized.har (next to the input; override with -o).
ALWAYS open the result in a text editor and search for your own data before
sharing it. This script is best-effort, not a guarantee.
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from pathlib import Path

R = "<redacted>"

# Parameter / JSON-key names whose VALUES are always removed (case-insensitive).
SENSITIVE_KEYS = {
    "password", "passwd", "pwd", "pass", "secret", "client_secret", "token",
    "access_token", "refresh_token", "id_token", "code", "otp", "tan", "totp",
    "pin", "code_verifier", "session_state", "state", "nonce", "sid",
    "sessionid", "session", "jsessionid", "csrf", "csrftoken", "xsrf",
    "authenticity_token", "__requestverificationtoken", "username", "user",
    "login", "loginname", "email", "mail", "signature", "sig", "assertion",
    "samlresponse", "samlrequest", "relaystate", "kc_idp_hint", "login_hint",
    "id_token_hint", "device_code", "user_code", "verification", "credential",
    "credentials", "apikey", "api_key", "key", "auth", "authorization",
}
# JWT claims that are safe & useful (structure/lifetime, no personal data).
SAFE_JWT_CLAIMS = {"iss", "aud", "exp", "iat", "nbf", "auth_time", "scope",
                   "scp", "typ", "azp", "acr", "amr", "token_use", "client_id"}

JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
ZP_RE = re.compile(r"\bCH\d{20,40}\b")  # Swiss metering point id
_keys = "|".join(sorted(map(re.escape, SENSITIVE_KEYS), key=len, reverse=True))
KV_RE = re.compile(rf"(?i)(?<![\w-])({_keys})(=|%3D)([^&\s\"';#]*)")
HIDDEN_INPUT_RE = re.compile(
    r"(?is)(<input\b[^>]*\btype\s*=\s*[\"']?(?:hidden|password)[\"']?[^>]*?\bvalue\s*=\s*)([\"'])(.*?)\2"
)
OPAQUE_RE = re.compile(r"[A-Za-z0-9_\-+/=.%]{24,}")

DROP_BODY_MIME = ("javascript", "css", "image/", "font", "wasm", "octet-stream",
                  "spreadsheet", "excel", "csv", "pdf", "zip", "video", "audio")
KEEP_BODY_MIME = ("json", "html", "xml", "text/plain", "x-www-form-urlencoded")


class Scrubber:
    def __init__(self, terms: list[str]):
        terms = [t for t in (x.strip() for x in terms) if t]
        # longest first so "Max Muster" wins over "Max"
        self.term_res = [re.compile(re.escape(t), re.I)
                         for t in sorted(terms, key=len, reverse=True)]
        self.counts = {"jwt": 0, "kv": 0, "term": 0, "email": 0, "zp": 0}

    # -- JWT -------------------------------------------------------------
    def _jwt(self, m: re.Match) -> str:
        self.counts["jwt"] += 1
        try:
            part = m.group(0).split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
            keep = {k: v for k, v in claims.items() if k in SAFE_JWT_CLAIMS}
            keep["_other_claim_names"] = sorted(set(claims) - SAFE_JWT_CLAIMS)
            keep = json.loads(self.text(json.dumps(keep), jwt=False))
            return "<JWT " + json.dumps(keep, separators=(",", ":")) + ">"
        except Exception:
            return "<JWT>"

    # -- free text (URLs, form bodies, HTML, header values) ---------------
    def text(self, s: str, jwt: bool = True) -> str:
        if jwt:
            s = JWT_RE.sub(self._jwt, s)

        def kv(m: re.Match) -> str:
            if m.group(3).startswith("<"):
                return m.group(0)
            self.counts["kv"] += 1
            return f"{m.group(1)}{m.group(2)}{R}"

        s = KV_RE.sub(kv, s)
        s = HIDDEN_INPUT_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{R}{m.group(2)}", s)
        n = len(ZP_RE.findall(s))
        if n:
            self.counts["zp"] += n
            s = ZP_RE.sub("<metering-point>", s)
        n = len(EMAIL_RE.findall(s))
        if n:
            self.counts["email"] += n
            s = EMAIL_RE.sub("<email>", s)
        for rx in self.term_res:
            s, n = rx.subn("<pii>", s)
            self.counts["term"] += n
        return s

    # -- JSON structures -------------------------------------------------
    def walk(self, obj):
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                if k.lower() in SENSITIVE_KEYS and not isinstance(v, (dict, list)):
                    out[k] = R if v not in (None, "", False, True) else v
                else:
                    out[self.text(k)] = self.walk(v)
            return out
        if isinstance(obj, list):
            return [self.walk(x) for x in obj]
        if isinstance(obj, str):
            return self.text(obj)
        return obj


def sensitive_header(name: str) -> bool:
    n = name.lower()
    return (n in {"authorization", "proxy-authorization", "cookie", "set-cookie",
                  "x-api-key"}
            or any(w in n for w in ("token", "csrf", "xsrf", "secret", "session")))


def clean_set_cookie(value: str, sc: Scrubber) -> str:
    """Keep cookie name + all attributes (Max-Age/Expires/Path/...); drop value."""
    first, *attrs = value.split(";")
    name = first.split("=", 1)[0].strip()
    return "; ".join([f"{name}={R}", *[sc.text(a.strip()) for a in attrs]])


def clean_headers(headers: list[dict], sc: Scrubber) -> list[dict]:
    out = []
    for h in headers:
        name, value = h.get("name", ""), h.get("value", "")
        ln = name.lower()
        if ln == "set-cookie":
            value = clean_set_cookie(value, sc)
        elif ln == "cookie":
            names = [p.split("=", 1)[0].strip() for p in value.split(";")]
            value = "; ".join(f"{n}={R}" for n in names if n)
        elif ln in {"authorization", "proxy-authorization"}:
            scheme, _, rest = value.partition(" ")
            value = f"{scheme} {sc.text(rest) if JWT_RE.search(rest) else R}"
        elif sensitive_header(name):
            value = R
        else:
            value = sc.text(value)
        out.append({**h, "name": name, "value": value})
    return out


def clean_body(text: str, mime: str, sc: Scrubber) -> str:
    m = (mime or "").lower()
    if "json" in m or text.lstrip()[:1] in "{[":
        try:
            return json.dumps(sc.walk(json.loads(text)), ensure_ascii=False)
        except ValueError:
            pass
    return sc.text(text)


def clean_content(content: dict, sc: Scrubber) -> dict:
    mime = (content.get("mimeType") or "").lower()
    text = content.get("text")
    out = {k: v for k, v in content.items() if k != "text"}
    if text is None:
        return out
    if content.get("encoding") == "base64":
        try:
            raw = base64.b64decode(text)
            if any(k in mime for k in KEEP_BODY_MIME) and not any(k in mime for k in DROP_BODY_MIME):
                text = raw.decode("utf-8", "replace")
                out.pop("encoding", None)
            else:
                out["text"] = f"<body omitted: {len(raw)} bytes, {mime or 'unknown type'}>"
                return out
        except Exception:
            out["text"] = "<body omitted: undecodable>"
            return out
    if any(k in mime for k in DROP_BODY_MIME) or not any(k in mime for k in KEEP_BODY_MIME):
        out["text"] = f"<body omitted: {len(text)} chars, {mime or 'unknown type'}>"
        return out
    out["text"] = clean_body(text, mime, sc)
    return out


def clean_entry(e: dict, sc: Scrubber) -> dict:
    req, res = e.get("request", {}), e.get("response", {})
    # request
    req["url"] = sc.text(req.get("url", ""))
    req["headers"] = clean_headers(req.get("headers", []), sc)
    req["queryString"] = [
        {**q, "value": R if q["name"].lower() in SENSITIVE_KEYS else sc.text(q.get("value", ""))}
        for q in req.get("queryString", [])
    ]
    req["cookies"] = [{**c, "value": R} for c in req.get("cookies", [])]
    pd = req.get("postData")
    if pd:
        mime = pd.get("mimeType", "")
        if pd.get("params"):
            pd["params"] = [
                {**p, "value": R if p["name"].lower() in SENSITIVE_KEYS else sc.text(p.get("value", ""))}
                for p in pd["params"]
            ]
        if "text" in pd:
            pd["text"] = clean_body(pd["text"], mime, sc)
    # response
    res["headers"] = clean_headers(res.get("headers", []), sc)
    res["cookies"] = [{**c, "value": R} for c in res.get("cookies", [])]
    if res.get("redirectURL"):
        res["redirectURL"] = sc.text(res["redirectURL"])
    res["content"] = clean_content(res.get("content", {}), sc)
    # misc chrome-specific fields can carry urls/stacks
    for k in [k for k in e if k.startswith("_") and k not in ("_resourceType",)]:
        e[k] = sc.walk(e[k])
    e["request"], e["response"] = req, res
    return e


def leftover_scan(data: str, terms: list[str]) -> tuple[list[str], list[str]]:
    hits = [t for t in terms if t.strip() and re.search(re.escape(t), data, re.I)]
    opaque = sorted({m for m in OPAQUE_RE.findall(data)
                     if re.search(r"\d", m) and re.search(r"[A-Za-z]", m)
                     and not m.startswith(("http", "<"))})
    return hits, opaque


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("har", type=Path)
    ap.add_argument("-o", "--output", type=Path)
    ap.add_argument("--redact", action="append", default=[], metavar="TEXT",
                    help="literal text to remove everywhere (name, address, customer no., user name). Repeatable.")
    args = ap.parse_args()

    har = json.loads(args.har.read_text(encoding="utf-8-sig"))
    log = har.get("log", {})
    sc = Scrubber(args.redact)

    log["entries"] = [clean_entry(e, sc) for e in log.get("entries", [])]
    for p in log.get("pages", []):
        p["title"] = sc.text(p.get("title", ""))
    log.pop("comment", None)
    har["log"] = log

    out = args.output or args.har.with_suffix(".sanitized.har")
    dumped = json.dumps(har, ensure_ascii=False, indent=1)
    out.write_text(dumped, encoding="utf-8")

    hits, opaque = leftover_scan(dumped, args.redact)
    print(f"Wrote {out}  ({len(log['entries'])} requests)")
    print("Redactions:", ", ".join(f"{k}={v}" for k, v in sc.counts.items()))
    if hits:
        print("!! WARNING: these --redact terms are STILL present:", hits)
    if opaque:
        print(f"\nReview: {len(opaque)} long opaque strings remain (may be harmless ids, "
              "or secrets). First 15:")
        for s in opaque[:15]:
            print("  ", s[:60] + ("…" if len(s) > 60 else ""))
        print("Search the output for any you do not recognise as harmless and add them via --redact.")
    print("\nNow open the file in an editor and search for your name, address, user name, "
          "e-mail and phone number before sharing.")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
