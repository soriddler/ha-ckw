#!/usr/bin/env python3
"""Smoke-test the ckw_client library against the real portal using a saved offline refresh token.

    .venv/bin/python tools/live_check.py ~/.ckw_probe_offline.json [--days 3 | --history 365]

Prints counts and sums only (no tokens, names or addresses). The rotated refresh token is written
back to the state file (same format as tools/probe_login.py).
"""
import argparse
import asyncio
import json
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "custom_components" / "ckw"))

import aiohttp  # noqa: E402

from ckw_client import CkwClient, CkwError, TokenManager, aggregate_hourly  # noqa: E402


def month_chunks(start: date, end: date):
    cur = start
    while cur <= end:
        nxt = (cur.replace(day=1) + timedelta(days=32)).replace(day=1)
        yield cur, min(end, nxt - timedelta(days=1))
        cur = nxt


async def history(client, c, start: date, end: date) -> None:
    """One line per month: does the portal return data for it?"""
    print(f"month-by-month {start}..{end} (what the integration's first import asks for)")
    for a, b in month_chunks(start, end):
        try:
            q = await client.quarter_hours(c, a, b)
            ms, _ = await client.measurements(c, a, b, "HOUR")
        except CkwError as err:
            print(f"  {a:%Y-%m}: FAILED {type(err).__name__}: {err}")
            continue
        hours = aggregate_hourly(q)
        have = [m for m in ms if m.kwh is not None]
        print(f"  {a:%Y-%m}: CSV {len(q):5d} quarter hours / {len(hours):4d} hours / {sum(h.kwh for h in hours):8.2f} kWh"
              f"   JSON {len(have):4d} hours / cost {sum(m.energy_cost_chf or 0 for m in have):7.2f} CHF")
        await asyncio.sleep(1)


def save(path: str, token: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump({"refresh_token": token, "saved": int(time.time())}, f)


async def main(path: str, days: int, history_days: int = 0) -> int:
    state = json.load(open(path))

    async def persist(tok: str) -> None:
        save(path, tok)
        print("   (rotated refresh token saved)")

    async with aiohttp.ClientSession() as session:
        client = CkwClient(session, TokenManager(session, state["refresh_token"], persist))
        try:
            contracts = await client.contracts()
            print(f"contracts: {len(contracts)}  group types: {[c.meter_group_type for c in contracts]}")
            c = contracts[0]
            if history_days:
                await history(client, c, date.today() - timedelta(days=history_days), date.today())
                return 0
            end = date.today()
            start = end - timedelta(days=days)

            q = await client.quarter_hours(c, start, end)
            hours = aggregate_hourly(q)
            print(f"CSV   {start}..{end}: {len(q)} quarter hours, {len(hours)} complete hours, "
                  f"{sum(h.kwh for h in hours):.3f} kWh, last = {q[-1].start.isoformat() if q else None}")

            ms, peaks = await client.measurements(c, start, end, "HOUR")
            have = [m for m in ms if m.kwh is not None]
            print(f"JSON  {start}..{end}: {len(ms)} hours, {len(have)} with data, "
                  f"{sum(m.kwh for m in have):.2f} kWh, energy cost {sum(m.energy_cost_chf or 0 for m in have):.2f} CHF, "
                  f"max peak {max((m.peak_kw or 0 for m in have), default=0)} kW")
            print(f"monthly peaks: {[(p.at.isoformat(), p.kw, p.charge_chf) for p in peaks]}")

            by_start = {h.start: h.kwh for h in hours}
            diffs = [abs(by_start[m.start] - m.kwh) for m in have if m.start in by_start]
            print(f"CSV vs JSON hourly kWh: {len(diffs)} common hours, max abs diff {max(diffs, default=0):.4f} "
                  f"(expected <= 0.005 from JSON rounding)")
            missing = [m.start for m in have if m.start not in by_start]
            print(f"JSON hours with data but not in CSV-complete hours: {len(missing)}")
        except CkwError as err:
            print(f"FAILED: {type(err).__name__}: {err}")
            return 1
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("state")
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--history", type=int, default=0, metavar="DAYS",
                    help="instead: month-by-month availability check for the last DAYS days (e.g. 365)")
    a = ap.parse_args()
    sys.exit(asyncio.run(main(a.state, a.days, a.history)))
