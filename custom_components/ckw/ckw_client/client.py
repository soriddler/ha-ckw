"""Authenticated access to the CKW data API."""
from __future__ import annotations

import asyncio
from datetime import date

import aiohttp

from .auth import TokenManager
from .const import API_BASE, REQUEST_TIMEOUT, USER_AGENT
from .exceptions import CannotConnect, LoginFlowError, ParseError, ReauthRequired
from .models import Contract, Measurement, MonthlyPeak, QuarterHour
from .parsers import parse_measurements, parse_monthly_peaks, parse_quarter_hour_csv

_TIMEOUT = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT)
_MEASUREMENTS = "/measurement-api/api/v1/electricityconsumption/measurements/location"
_CSV_EXPORT = "/measurement-api/api/v1/file/location"


def _date(raw: object) -> date | None:
    try:
        return date.fromisoformat(str(raw))
    except ValueError:
        return None


class CkwClient:
    def __init__(self, session: aiohttp.ClientSession, tokens: TokenManager) -> None:
        self._session = session
        self._tokens = tokens
        self._gpnr: str | None = None

    async def _call(self, method: str, path: str, *, json: dict | None = None, gpnr: bool = False) -> bytes:
        for attempt in (0, 1):
            headers = {
                "Authorization": f"Bearer {await self._tokens.access_token(force_refresh=attempt == 1)}",
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://my.ckw.ch",
                "Referer": "https://my.ckw.ch/",
                "User-Agent": USER_AGENT,
            }
            if gpnr:
                headers["x-ckw-gpnr"] = await self.gpnr()
            try:
                async with self._session.request(
                    method, f"{API_BASE}{path}", json=json, headers=headers, timeout=_TIMEOUT
                ) as resp:
                    if resp.status == 401 and attempt == 0:
                        continue  # access token rejected: refresh once and retry
                    if resp.status == 401:
                        raise ReauthRequired("API keeps answering 401 with a fresh token")
                    if resp.status != 200:
                        raise CannotConnect(f"{method} {path}: HTTP {resp.status}")
                    return await resp.read()
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                raise CannotConnect(f"{method} {path}: {err}") from err
        raise CannotConnect("unreachable")  # pragma: no cover

    async def _json(self, method: str, path: str, **kw) -> dict:
        import json

        try:
            data = json.loads(await self._call(method, path, **kw))
        except ValueError as err:
            raise ParseError(f"{path}: response is not JSON") from err
        if not isinstance(data, dict):
            raise ParseError(f"{path}: unexpected JSON type")
        return data

    async def gpnr(self) -> str:
        """Business-partner number; required as ``x-ckw-gpnr`` header on measurement calls."""
        if self._gpnr is None:
            data = await self._json("GET", "/account-api/api/v1/account")
            try:
                accounts = data["body"]["accounts"]
                main = next((a for a in accounts if a.get("mainAccount")), accounts[0])
                self._gpnr = str(main["gpnr"])
            except (KeyError, IndexError, TypeError) as err:
                raise ParseError("no account found") from err
        return self._gpnr

    async def contracts(self) -> list[Contract]:
        data = await self._json("GET", "/account-api/api/v2/account/contracts?market=regulated", gpnr=True)
        try:
            rows = data["body"]["activeContracts"]
            return [
                Contract(
                    location_id=str(r["locationId"]),
                    meter_id=str(r["meterId"]),
                    meter_group_type=str(r["activeMeterGroupType"]),
                    label=str(r.get("label") or r.get("contractDescription") or ""),
                    supply_start=_date(r.get("supplyStartDate")),
                )
                for r in rows
                if not r.get("isFeedIn") and not r.get("isProduction")
            ]
        except (KeyError, TypeError) as err:
            raise ParseError("unexpected contracts response") from err

    @staticmethod
    def _body(contract: Contract, aggregation: str, start: date, end: date) -> dict:
        return {
            "locationId": contract.location_id,
            "activeMeterGroupType": contract.meter_group_type,
            "aggregation": aggregation,
            "startDate": start.isoformat(),
            "endDate": end.isoformat(),
        }

    async def quarter_hours(self, contract: Contract, start: date, end: date) -> list[QuarterHour]:
        """15-minute kWh values from the CSV export (full precision)."""
        raw = await self._call(
            "POST", _CSV_EXPORT, json=self._body(contract, "FIFTEEN_MINUTES", start, end), gpnr=True
        )
        return parse_quarter_hour_csv(raw.decode("utf-8-sig"))

    async def measurements(
        self, contract: Contract, start: date, end: date, aggregation: str = "HOUR"
    ) -> tuple[list[Measurement], list[MonthlyPeak]]:
        """Hourly (or daily) kWh, peak kW and energy cost from the JSON API (2 decimals)."""
        if aggregation not in ("HOUR", "DAY"):
            raise LoginFlowError(f"unsupported aggregation {aggregation}")
        data = await self._json(
            "POST", _MEASUREMENTS, json=self._body(contract, aggregation, start, end), gpnr=True
        )
        return parse_measurements(data), parse_monthly_peaks(data)
