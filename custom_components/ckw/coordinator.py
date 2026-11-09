"""Daily data update for one CKW contract."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.components.recorder import get_instance
from homeassistant.core import CALLBACK_TYPE, CoreState, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .ckw_client import (
    CannotConnect,
    CkwClient,
    CkwError,
    Contract,
    Measurement,
    ReauthRequired,
    aggregate_hourly,
)
from .const import (
    BACKFILL_DAYS,
    CONF_HISTORY_MONTHS,
    CONF_LOCATION_ID,
    CONF_METER_GROUP_TYPE,
    CONF_METER_ID,
    DOMAIN,
    MAX_RETRIES,
    OVERLAP_DAYS,
    RENEW_LOGIN_BEFORE,
    REQUEST_PAUSE,
    RETRY_ON_ERROR,
    RETRY_ON_STALE,
)
from .statistics_import import (
    COST,
    ENERGY,
    PEAK,
    cost_meta,
    energy_meta,
    import_cumulative,
    import_peaks,
    last_stat_start,
    peak_meta,
    statistic_id,
)
from .stats import chunks, cost_values, energy_values, local_date, peak_rows

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class CkwData:
    """Values shown by the sensors: the newest day that has data, and the running month."""

    day: date | None
    day_energy_kwh: float | None
    day_peak_kw: float | None
    day_cost_chf: float | None
    month_peak_kw: float | None
    month_peak_at: datetime | None
    month_power_charge_chf: float | None
    last_data: datetime | None
    login_expires: datetime | None = None  # when the CKW login ends and a new SMS login is needed


class CkwCoordinator(DataUpdateCoordinator[CkwData]):
    config_entry: ConfigEntry

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client: CkwClient) -> None:
        # update_interval=None: runs are triggered by a daily time schedule (see __init__.py) and retries
        super().__init__(hass, _LOGGER, name=DOMAIN, config_entry=entry, update_interval=None)
        self.client = client
        self.contract = Contract(
            location_id=entry.data[CONF_LOCATION_ID],
            meter_id=entry.data.get(CONF_METER_ID, ""),
            meter_group_type=entry.data[CONF_METER_GROUP_TYPE],
            label=entry.title,
        )
        self._retry: CALLBACK_TYPE | None = None
        self._attempts = 0
        self._lock = asyncio.Lock()  # one import at a time (daily run vs. manual re-import)
        self._reimport: tuple[date | None] | None = None  # set by async_reimport: (start or None=auto,)

    # -------------------------------------------------------------- retry handling --
    @callback
    def cancel_retry(self) -> None:
        if self._retry:
            self._retry()
            self._retry = None

    def _schedule_retry(self, delay: timedelta, why: str) -> None:
        self.cancel_retry()
        if self._attempts >= MAX_RETRIES:
            _LOGGER.warning("Giving up retrying today (%s); next run at the daily schedule", why)
            return
        self._attempts += 1
        _LOGGER.debug("Retry %s in %s: %s", self._attempts, delay, why)

        async def _again(_now: datetime) -> None:
            self._retry = None
            await self.async_refresh()

        self._retry = async_call_later(self.hass, delay.total_seconds(), _again)

    # -------------------------------------------------------------------- update --
    async def _supply_start(self) -> date | None:
        """Start of the supply contract = earliest possible data (None if the portal does not say)."""
        try:
            for c in await self.client.contracts():
                if c.location_id == self.contract.location_id and c.supply_start:
                    return max(c.supply_start, date(2015, 1, 1))
        except (CannotConnect, ReauthRequired):
            raise
        except CkwError:
            pass
        return None

    async def _history_start(self, today: date, everything: bool = False) -> date:
        """Where a full import begins: the amount chosen at setup (0 = everything), never before the supply start."""
        months = 0 if everything else int(self.config_entry.data.get(CONF_HISTORY_MONTHS, 0))
        supply = await self._supply_start()
        if months <= 0:
            return supply or today - timedelta(days=BACKFILL_DAYS)
        total = today.year * 12 + today.month - 1 - months
        start = date(total // 12, total % 12 + 1, 1)
        return max(start, supply) if supply else start

    async def _window_start(self, today: date) -> tuple[date, bool, bool]:
        """(first day to fetch, is a full import, running totals start from zero)."""
        if self._reimport is not None:
            (requested,) = self._reimport
            self._reimport = None
            return requested or await self._history_start(today, everything=True), True, True
        last = await last_stat_start(self.hass, statistic_id(ENERGY, self.contract.location_id))
        if last is None:
            return await self._history_start(today), True, False
        return local_date(last) - timedelta(days=OVERLAP_DAYS), False, False

    async def has_history(self) -> bool:
        return await last_stat_start(self.hass, statistic_id(ENERGY, self.contract.location_id)) is not None

    async def async_reimport(self, start: date | None = None) -> None:
        """Delete the imported statistics and import everything again from ``start`` (default: supply start).

        Needed to add OLDER data: the stored running total starts at the first imported hour, so history
        cannot be put in front of it without rebuilding the totals.
        """
        loc = self.contract.location_id
        self._reimport = (start,)
        # queued before the new import, so the recorder processes it first
        get_instance(self.hass).async_clear_statistics([statistic_id(k, loc) for k in (ENERGY, COST, PEAK)])
        await self.async_refresh()

    async def _async_update_data(self) -> CkwData:
        async with self._lock:
            return await self._update()

    async def _update(self) -> CkwData:
        if self.hass.state is CoreState.running:
            # a previous run may still be queued in the recorder; read its result, not stale data.
            # Not during startup: the recorder holds commits until startup is done (would deadlock).
            await get_instance(self.hass).async_block_till_done()
        loc = self.contract.location_id
        today = dt_util.now().date()
        try:
            start, full, fresh = await self._window_start(today)
            if full:
                _LOGGER.info("CKW: full import from %s to %s (this takes a while)", start, today)
            month_peaks, last_ms, last_hours = [], [], []
            newest_day: date | None = None  # newest day with complete CSV hours (the JSON lags behind)
            # running totals handed from chunk to chunk; 0.0 = known to start from zero, None = read from the DB
            carry: dict[str, float | None] = {ENERGY: 0.0 if fresh else None, COST: 0.0 if fresh else None}
            for i, (c_start, c_end) in enumerate(chunks(start, today)):
                if i and full:
                    await asyncio.sleep(REQUEST_PAUSE)
                quarters = await self.client.quarter_hours(self.contract, c_start, c_end)
                ms, peaks = await self.client.measurements(self.contract, c_start, c_end, "HOUR")
                hours = aggregate_hourly(quarters)
                # import chunk by chunk (oldest first) so a failure keeps the progress made
                for kind, meta, values in (
                    (ENERGY, energy_meta(loc), energy_values(hours)),
                    (COST, cost_meta(loc), cost_values(ms)),
                ):
                    total = await import_cumulative(self.hass, meta, values, carry[kind])
                    carry[kind] = total if total is not None else carry[kind]
                await import_peaks(self.hass, peak_meta(loc), peak_rows(ms))
                _LOGGER.info(
                    "CKW %s..%s: %d quarter hours, %d complete hours, %d cost hours, %d peak hours",
                    c_start, c_end, len(quarters), len(hours), len(cost_values(ms)), len(peak_rows(ms)),
                )
                month_peaks, last_ms, last_hours = peaks, ms, hours
                if hours:
                    newest_day = max(newest_day or date.min, local_date(hours[-1].start))
        except ReauthRequired as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except CannotConnect as err:
            self._schedule_retry(RETRY_ON_ERROR, str(err))
            raise UpdateFailed(f"Cannot reach CKW: {err}") from err
        except CkwError as err:
            self._schedule_retry(RETRY_ON_ERROR, str(err))
            raise UpdateFailed(f"CKW returned unexpected data: {err}") from err

        data = self._build_data(last_ms, last_hours, month_peaks)
        expires = getattr(self.client, "session_expires_at", None)
        if expires:
            data = replace(data, login_expires=dt_util.utc_from_timestamp(expires))
            if dt_util.utcnow() >= data.login_expires - RENEW_LOGIN_BEFORE:
                # the deadline is fixed; log in again NOW (one SMS) so the data never stops
                _LOGGER.warning("CKW login ends on %s; asking for a new login", data.login_expires.isoformat())
                self.config_entry.async_start_reauth(self.hass)
        latest = max(filter(None, (newest_day, data.day)), default=None)
        if latest is None or latest < today - timedelta(days=1):
            self._schedule_retry(RETRY_ON_STALE, "newest data older than yesterday")
        else:
            self._attempts = 0
            self.cancel_retry()
        return data

    @staticmethod
    def _build_data(ms: list[Measurement], hours, month_peaks) -> CkwData:
        with_data = [m for m in ms if m.kwh is not None]
        peak = month_peaks[-1] if month_peaks else None
        if not with_data:
            return CkwData(None, None, None, None, peak.kw if peak else None, peak.at if peak else None,
                           peak.charge_chf if peak else None, None)
        # newest local day with data; prefer a complete day (24h, 23h or 25h on DST days)
        by_day: dict[date, list[Measurement]] = {}
        for m in with_data:
            by_day.setdefault(local_date(m.start), []).append(m)
        day = max(by_day)
        if len(by_day[day]) < 23 and len(by_day) > 1:
            day = sorted(by_day)[-2]
        day_ms = by_day[day]
        csv_kwh = sum(h.kwh for h in hours if local_date(h.start) == day)
        return CkwData(
            day=day,
            day_energy_kwh=round(csv_kwh, 3) if csv_kwh else round(sum(m.kwh for m in day_ms), 3),
            day_peak_kw=max((m.peak_kw for m in day_ms if m.peak_kw is not None), default=None),
            day_cost_chf=round(sum(m.energy_cost_chf or 0 for m in day_ms), 2),
            month_peak_kw=peak.kw if peak else None,
            month_peak_at=peak.at if peak else None,
            month_power_charge_chf=peak.charge_chf if peak else None,
            last_data=max(m.start for m in with_data) + timedelta(hours=1),
        )
