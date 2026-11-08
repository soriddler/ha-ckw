#!/usr/bin/env python3
"""Build scrubbed test fixtures in tests/fixtures/ from example_data/ (not committed).

Run:  python3 -I tools/make_fixtures.py
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EX, OUT = ROOT / "example_data", ROOT / "tests" / "fixtures"
OUT.mkdir(parents=True, exist_ok=True)


def entries(name):
    return json.loads((EX / name).read_text())["log"]["entries"]


def scrub_html(t: str) -> str:
    """Keep only the kcContext script and neutralise ids/phone."""
    i = t.index("const kcContext")
    j = t.index("kcContext.keycloakifyVersion")
    t = t[i:j]
    t = re.sub(r"session_code=[\w-]+", "session_code=SESSION_CODE", t)
    t = re.sub(r"tab_id=[\w-]+", "tab_id=TAB_ID", t)
    t = re.sub(r"client_data=[\w-]+", "client_data=CLIENT_DATA", t)
    t = re.sub(r'"authSessionId(Hash)?": "[^"]*"', lambda m: f'"authSessionId{m.group(1) or ""}": "X"', t)
    t = re.sub(r'"tabId": "[^"]*"', '"tabId": "TAB_ID"', t)
    t = re.sub(r"\*{4,}\d{2,4}", "*********0000", t)
    t = re.sub(r'"attemptedUsername": "[^"]*"', '"attemptedUsername": "user@example.org"', t)
    return t


def html_of(har, idx=0):
    return entries(har)[idx]["response"]["content"]["text"]


pages = {
    "kc_after_password_sms_page.js": ("id.ckw.ch_login_wo_tan.sanitized.har", 0),
    "kc_wrong_password.js": ("id.ckw.ch_login_wrong_password.sanitized.har", 0),
    "kc_wrong_sms_code.js": ("id.ckw.ch_login_wrong_smscode.sanitized.har", 0),
}
for name, (har, idx) in pages.items():
    (OUT / name).write_text(scrub_html(html_of(har, idx)))

# JSON api responses
es = entries("my.ckw.ch.sanitized.har")


def body(i):
    return json.loads(es[i]["response"]["content"]["text"])


# JSON keys whose values identify the customer or the premises -> replaced by fake values
FAKE_BY_KEY = {
    "label": "Musterstrasse 1 , 6000 Luzern", "secondaryLabel": "Musterwohnung",
    "contractDescription": "Musterwohnung", "streetAddress": "Musterstrasse 1", "city": "Luzern",
    "postalCode": "6000", "contractId": "1000002", "premiseId": "1000003", "contractAccountId": "1000004",
    "locationId": "1000001",
}


def _fake(o):
    if isinstance(o, dict):
        return {k: FAKE_BY_KEY.get(k, _fake(v)) if not isinstance(v, (dict, list)) else _fake(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_fake(x) for x in o]
    return o


def scrub_json(o):
    s = json.dumps(o, ensure_ascii=False)
    s = s.replace("<pii>", "Example").replace("<metering-point>", "CH0000000000000000000000000000000")
    return _fake(json.loads(s))


for fname, i in {"api_account.json": 23, "api_contracts.json": 32, "api_hourly_2026-11-07.json": 107,
                 "api_daily_2026-11.json": 37}.items():
    (OUT / fname).write_text(json.dumps(scrub_json(body(i)), indent=1, ensure_ascii=False))

# CSV: anonymise the header block
src = (EX / "15-Minuten Werte - November 2026.csv").read_text(encoding="utf-8-sig")
lines = src.splitlines()
anon = {"Kunde": "Kunde: Max Muster", "Kundennummer": "Kundennummer: 1000000",
        "Adresse": "Adresse: Musterstrasse 1 , 6000 Luzern", "Vertrag": "Vertrag: Musterwohnung",
        "Zählpunkt": "Zählpunkt: CH0000000000000000000000000000000"}
out = []
for ln in lines:
    key = ln.split(":", 1)[0]
    out.append(anon.get(key, ln))
(OUT / "quarter_hour_2026-11.csv").write_text("﻿" + "\n".join(out) + "\n", encoding="utf-8")
print("fixtures written:", sorted(p.name for p in OUT.iterdir()))
