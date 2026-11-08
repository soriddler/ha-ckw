"""Parsers for the CSV export and the JSON measurement API."""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .exceptions import ParseError
from .models import HourlyEnergy, Measurement, MonthlyPeak, QuarterHour

LOCAL_TZ = ZoneInfo("Europe/Zurich")
_MISSING = {"", "-", "nodata", "null", "none"}


class _LocalClock:
    """Turn a sequence of naive local timestamps into aware UTC datetimes.

    On the autumn DST change the local hour 02:00-02:59 occurs twice. The portal lists rows in
    chronological order, so a timestamp that does not advance marks the second pass (fold=1).
    """

    def __init__(self) -> None:
        self._prev: datetime | None = None
        self._fold_until: datetime | None = None

    def to_utc(self, naive: datetime) -> datetime:
        fold = 0
        if self._fold_until is not None and naive <= self._fold_until:
            fold = 1
        elif self._prev is not None and naive <= self._prev:
            self._fold_until = self._prev
            fold = 1
        else:
            self._fold_until = None
        self._prev = naive
        return naive.replace(tzinfo=LOCAL_TZ, fold=fold).astimezone(timezone.utc)


def _number(raw: object) -> float | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if s.lower() in _MISSING:
        return None
    try:
        return float(s.replace(",", "."))
    except ValueError as err:
        raise ParseError(f"not a number: {s!r}") from err


# ---------------------------------------------------------------- CSV (15 minutes) ----
_CSV_ROW = re.compile(r"^(\d{2})\.(\d{2})\.(\d{4}) (\d{2}):(\d{2});(.*)$")


def parse_quarter_hour_csv(text: str) -> list[QuarterHour]:
    """Parse the '15-Minuten Werte' export. Rows without data ('-') are skipped, never zero."""
    text = text.lstrip("﻿")
    lines = text.splitlines()
    try:
        header_idx = next(i for i, ln in enumerate(lines) if ln.startswith("Zeitraum;"))
    except StopIteration as err:
        raise ParseError("CSV header 'Zeitraum;...' not found") from err
    if "Energieverbrauch (kWh)" not in lines[header_idx]:
        raise ParseError(f"unexpected CSV columns: {lines[header_idx]!r}")

    clock = _LocalClock()
    out: list[QuarterHour] = []
    for ln in lines[header_idx + 1 :]:
        if not ln.strip():
            continue
        if ln.startswith("Total"):
            break
        m = _CSV_ROW.match(ln)
        if not m:
            raise ParseError(f"unexpected CSV row: {ln[:60]!r}")
        d, mo, y, hh, mi, raw = m.groups()
        naive = datetime(int(y), int(mo), int(d), int(hh), int(mi))
        start = clock.to_utc(naive)  # advance the clock for every row, also missing ones
        value = _number(raw)
        if value is not None:
            out.append(QuarterHour(start=start, kwh=round(value, 4)))
    return out


def aggregate_hourly(readings: list[QuarterHour]) -> list[HourlyEnergy]:
    """Sum quarter hours to full hours. Hours with fewer than 4 quarters are dropped (incomplete)."""
    buckets: dict[datetime, dict[int, float]] = defaultdict(dict)
    for r in readings:
        hour = r.start.replace(minute=0, second=0, microsecond=0)
        buckets[hour][r.start.minute] = r.kwh
    return [
        HourlyEnergy(start=h, kwh=round(sum(q.values()), 4))
        for h, q in sorted(buckets.items())
        if set(q) == {0, 15, 30, 45}
    ]


# ---------------------------------------------------------------- JSON (hour / day) ----
def _local_start(raw: str, clock: _LocalClock) -> datetime:
    try:
        if "T" in raw:
            naive = datetime.strptime(raw, "%Y-%m-%dT%H:%M")
        else:
            naive = datetime.strptime(raw, "%Y-%m-%d")
    except ValueError as err:
        raise ParseError(f"bad timestamp {raw!r}") from err
    return clock.to_utc(naive)


def _body(data: dict) -> dict:
    try:
        if data["status"]["type"] != "SUCCESS":
            raise ParseError(f"API status {data['status'].get('type')}: {data['status'].get('message')}")
        return data["body"]
    except (KeyError, TypeError) as err:
        raise ParseError("unexpected API response structure") from err


def parse_measurements(data: dict) -> list[Measurement]:
    """Parse the response of ``electricityconsumption/measurements/location`` (HOUR or DAY)."""
    body = _body(data)
    try:
        rows = body["measurements"]
    except KeyError as err:
        raise ParseError("no 'measurements' in response") from err
    clock = _LocalClock()
    out: list[Measurement] = []
    for row in rows:
        amount = row.get("amount") or {}
        out.append(
            Measurement(
                start=_local_start(row["timestampStartOfPeriod"], clock),
                kwh=_number(row.get("energy")),
                peak_kw=_number(row.get("maxPower")),
                energy_cost_chf=_number(amount.get("energy")),
                power_charge_chf=_number(amount.get("maxPower")),
            )
        )
    return out


def parse_monthly_peaks(data: dict) -> list[MonthlyPeak]:
    """Peak power per month with its monthly charge (``maxPowerPerMonth``)."""
    body = _body(data)
    clock = _LocalClock()
    peaks = []
    for row in body.get("maxPowerPerMonth") or []:
        kw, chf = _number(row.get("maxPower")), _number(row.get("amount"))
        if kw is None:
            continue
        peaks.append(MonthlyPeak(at=_local_start(row["timestamp"], clock), kw=kw, charge_chf=chf or 0.0))
    return peaks
