"""Write CKW data into Home Assistant's long-term statistics (usable in the Energy dashboard)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMeanType, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import EnergyConverter, PowerConverter

from .const import DOMAIN
from .stats import MaxRow, SumRow, cumulative

ENERGY, COST, PEAK = "energy_consumption", "energy_cost", "peak_power"


def statistic_id(kind: str, location_id: str) -> str:
    return f"{DOMAIN}:{kind}_{location_id}"


def _meta(
    kind: str, location_id: str, name: str, unit: str, unit_class: str | None, has_sum: bool,
    mean_type: StatisticMeanType = StatisticMeanType.NONE,
) -> StatisticMetaData:
    return StatisticMetaData(
        mean_type=mean_type,
        has_sum=has_sum,
        name=name,
        source=DOMAIN,
        statistic_id=statistic_id(kind, location_id),
        unit_class=unit_class,
        unit_of_measurement=unit,
    )


def energy_meta(loc: str) -> StatisticMetaData:
    return _meta(ENERGY, loc, "CKW energy consumption", "kWh", EnergyConverter.UNIT_CLASS, True)


def cost_meta(loc: str) -> StatisticMetaData:
    return _meta(COST, loc, "CKW energy cost", "CHF", None, True)


def peak_meta(loc: str) -> StatisticMetaData:
    # Home Assistant keeps min/max only for statistics with a mean type, so the hourly peak is stored as
    # mean = min = max (one value per hour: the highest 15-minute average power).
    return _meta(PEAK, loc, "CKW peak power", "kW", PowerConverter.UNIT_CLASS, False, StatisticMeanType.ARITHMETIC)


async def summary(hass: HomeAssistant, stat_id: str) -> dict:
    """First/last hour, number of hours and newest running total of a statistic (for diagnostics)."""
    found = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, datetime(2000, 1, 1, tzinfo=timezone.utc), None, {stat_id}, "hour", None, {"sum", "max"}
    )
    rows = found.get(stat_id, [])
    if not rows:
        return {"hours": 0}
    last = rows[-1]
    return {
        "hours": len(rows),
        "first": dt_util.utc_from_timestamp(rows[0]["start"]).isoformat(),
        "last": dt_util.utc_from_timestamp(last["start"]).isoformat(),
        "last_sum_or_max": last.get("sum", last.get("max")),
    }


async def last_stat_start(hass: HomeAssistant, stat_id: str) -> datetime | None:
    """Start of the newest stored hour of a statistic, or None if there is none."""
    last = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, stat_id, False, {"sum"}
    )
    rows = last.get(stat_id)
    return dt_util.utc_from_timestamp(rows[0]["start"]) if rows else None


async def _sum_before(hass: HomeAssistant, stat_id: str, before: datetime) -> tuple[float, bool]:
    """Running total of the newest stored hour strictly before ``before``.

    Returns (base, append_only). append_only=True means: stored data exists but none before ``before``
    except a newest row at/after it, so rewriting from ``before`` is not possible without a base.
    """
    instance = get_instance(hass)
    for lookback in (timedelta(days=14), timedelta(days=4000)):
        found = await instance.async_add_executor_job(
            statistics_during_period, hass, before - lookback, before, {stat_id}, "hour", None, {"sum"}
        )
        rows = [r for r in found.get(stat_id, []) if r.get("sum") is not None]
        if rows:
            return float(rows[-1]["sum"]), False
    last = await instance.async_add_executor_job(get_last_statistics, hass, 1, stat_id, False, {"sum"})
    if last.get(stat_id):
        return 0.0, True  # data exists after `before` but nothing earlier: caller must not rewrite
    return 0.0, False


async def import_cumulative(
    hass: HomeAssistant,
    meta: StatisticMetaData,
    values: list[tuple[datetime, float]],
    base: float | None = None,
) -> float | None:
    """Import hourly increments as a running sum, continuing from what is already stored.

    ``base`` is the running total before the first value. Pass the return value of the previous call
    when importing consecutive chunks; if None it is read from the stored statistics.
    Hours already stored are overwritten (same start), later totals are recomputed, so re-importing
    an overlapping window never double counts. Returns the last running total (None: nothing imported).

    Never waits for the recorder to commit: during Home Assistant's startup the recorder holds back
    commits until startup is complete, and startup waits for this integration.
    """
    if not values:
        return None
    stat_id = meta["statistic_id"]
    values = sorted(values, key=lambda v: v[0])
    if base is None:
        base, append_only = await _sum_before(hass, stat_id, values[0][0])
        if append_only:
            # stored data exists after our first hour but nothing before it: only append, never rewrite
            newest = await last_stat_start(hass, stat_id)
            last = await get_instance(hass).async_add_executor_job(
                get_last_statistics, hass, 1, stat_id, False, {"sum"}
            )
            base = float(last[stat_id][0]["sum"])
            values = [v for v in values if newest is not None and v[0] > newest]
            if not values:
                return base
    rows: list[SumRow] = cumulative(values, base)
    async_add_external_statistics(
        hass,
        meta,
        [StatisticData(start=r.start, state=r.sum, sum=r.sum) for r in rows],
    )
    return rows[-1].sum


async def import_peaks(hass: HomeAssistant, meta: StatisticMetaData, rows: list[MaxRow]) -> int:
    if not rows:
        return 0
    async_add_external_statistics(
        hass, meta, [StatisticData(start=r.start.astimezone(timezone.utc), mean=r.max, min=r.max, max=r.max) for r in rows]
    )
    return len(rows)
