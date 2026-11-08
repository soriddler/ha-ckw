"""Plain data containers."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class Tokens:
    access_token: str
    refresh_token: str
    expires_at: float  # epoch seconds when the access token expires


@dataclass(frozen=True)
class Contract:
    location_id: str
    meter_id: str
    meter_group_type: str  # e.g. "BASIC"
    label: str
    supply_start: date | None = None  # start of the supply contract = earliest possible data


@dataclass(frozen=True)
class QuarterHour:
    start: datetime  # timezone-aware (UTC)
    kwh: float


@dataclass(frozen=True)
class HourlyEnergy:
    start: datetime  # timezone-aware (UTC), on the hour
    kwh: float


@dataclass(frozen=True)
class Measurement:
    """One period of the JSON API (hour or day). Missing values ('nodata') are None."""

    start: datetime  # timezone-aware (UTC)
    kwh: float | None
    peak_kw: float | None
    energy_cost_chf: float | None  # energy part only (amount.energy)
    power_charge_chf: float | None  # monthly peak-power charge if attributed to this period


@dataclass(frozen=True)
class MonthlyPeak:
    at: datetime  # when the monthly maximum occurred (UTC)
    kw: float
    charge_chf: float
