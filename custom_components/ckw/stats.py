"""Pure helpers that turn parsed readings into statistics rows (no Home Assistant imports)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from .ckw_client import HourlyEnergy, Measurement

LOCAL_TZ = ZoneInfo("Europe/Zurich")


@dataclass(frozen=True)
class SumRow:
    start: datetime
    sum: float  # running total including this hour


@dataclass(frozen=True)
class MaxRow:
    start: datetime
    max: float


def cumulative(values: list[tuple[datetime, float]], base: float = 0.0) -> list[SumRow]:
    """Running totals starting at ``base``. Duplicate hours: the last value wins."""
    unique = dict(sorted(values, key=lambda v: v[0]))
    rows, total = [], base
    for start, value in sorted(unique.items()):
        total = round(total + value, 6)
        rows.append(SumRow(start, total))
    return rows


def energy_values(hours: list[HourlyEnergy]) -> list[tuple[datetime, float]]:
    return [(h.start, h.kwh) for h in hours]


def cost_values(ms: list[Measurement]) -> list[tuple[datetime, float]]:
    """Energy cost per hour; hours without data (or without a price) are left out."""
    return [(m.start, m.energy_cost_chf) for m in ms if m.kwh is not None and m.energy_cost_chf is not None]


def peak_rows(ms: list[Measurement]) -> list[MaxRow]:
    return [MaxRow(m.start, m.peak_kw) for m in ms if m.kwh is not None and m.peak_kw is not None]


def local_date(dt: datetime) -> date:
    return dt.astimezone(LOCAL_TZ).date()


def chunks(start: date, end: date) -> list[tuple[date, date]]:
    """Split [start, end] at calendar-month boundaries (one portal request per month)."""
    out, cur = [], start
    while cur <= end:
        nxt = (cur.replace(day=1) + timedelta(days=32)).replace(day=1)
        out.append((cur, min(end, nxt - timedelta(days=1))))
        cur = nxt
    return out
