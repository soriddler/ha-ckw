#!/usr/bin/env python3
"""Measure how long CKW keeps an idle offline session alive.

1. Create a fresh session (one SMS):
     python3 -I tools/probe_login.py --username <login> --offline --save-state ~/.ckw_idle_A.json
2. Run one of:
     python3 -I tools/probe_idle.py ~/.ckw_idle_A.json --every 10      # refresh every 10 min, up to --hours
     python3 -I tools/probe_idle.py ~/.ckw_idle_B.json --escalate      # growing gaps: 5,10,20,30,45,60,90,120,180,240 min

Use two sessions at once (A: --every 10, B: --escalate) to tell "idle timeout" from "absolute session age".
Output: timestamps, gap since the previous successful refresh, result. No tokens or personal data.
Keep the terminal open (or run in tmux/screen). Stdlib only.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN = "https://id.ckw.ch/realms/CKW-prod/protocol/openid-connect/token"
GAPS_MIN = [5, 10, 20, 30, 45, 60, 90, 120, 180, 240]


def refresh(token):
    body = urllib.parse.urlencode({"grant_type": "refresh_token", "client_id": "dtp-web", "refresh_token": token}).encode()
    req = urllib.request.Request(TOKEN, data=body, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/149.0"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except ValueError:
            return e.code, {}
    except OSError as e:
        return 0, {"error": "network", "error_description": str(e)}


def save(path, token):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"refresh_token": token, "saved": int(time.time())}, f)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("state")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--every", type=int, metavar="MIN")
    g.add_argument("--escalate", action="store_true")
    ap.add_argument("--hours", type=float, default=10, help="stop after this many hours (with --every)")
    a = ap.parse_args()

    token = json.load(open(a.state))["refresh_token"]
    start = last_ok = time.time()
    gaps = iter(GAPS_MIN) if a.escalate else None
    n = 0
    print(f"{time.strftime('%H:%M:%S')} start (session assumed to be created just now)")
    while True:
        if a.escalate:
            gap = next(gaps, None)
            if gap is None:
                print("all gaps survived: idle timeout is longer than 4 h"); return 0
        else:
            gap = a.every
            if time.time() - start > a.hours * 3600:
                print(f"survived {a.hours} h with refreshes every {a.every} min"); return 0
        time.sleep(gap * 60)
        status, payload = refresh(token)
        now = time.time()
        age = (now - start) / 60
        idle = (now - last_ok) / 60
        n += 1
        if status == 200:
            token = payload["refresh_token"]
            save(a.state, token)
            last_ok = now
            print(f"{time.strftime('%H:%M:%S')} #{n:3d} OK     idle {idle:6.1f} min, session age {age:6.1f} min")
        elif status == 0:
            print(f"{time.strftime('%H:%M:%S')} #{n:3d} network problem, retrying: {payload['error_description'][:80]}")
            continue
        else:
            print(f"{time.strftime('%H:%M:%S')} #{n:3d} DEAD   idle {idle:6.1f} min, session age {age:6.1f} min -> "
                  f"{payload.get('error')}: {payload.get('error_description')}")
            print(f"=> idle timeout is between the last OK gap and {idle:.0f} min (or the session has a max age <= {age:.0f} min)")
            return 1


if __name__ == "__main__":
    sys.exit(main())
