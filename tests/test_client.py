import json
from datetime import date

import aiohttp
import pytest
from aioresponses import aioresponses

from ckw_client import CannotConnect, CkwClient, Contract, ParseError, ReauthRequired
from ckw_client.const import API_BASE

ACCOUNT = f"{API_BASE}/account-api/api/v1/account"
CONTRACTS = f"{API_BASE}/account-api/api/v2/account/contracts?market=regulated"
MEAS = f"{API_BASE}/measurement-api/api/v1/electricityconsumption/measurements/location"
CSV = f"{API_BASE}/measurement-api/api/v1/file/location"
CONTRACT = Contract("1000001", "CH000", "BASIC", "label")


class FakeTokens:
    def __init__(self):
        self.forced = 0

    async def access_token(self, *, force_refresh=False):
        if force_refresh:
            self.forced += 1
        return "TOKEN-2" if self.forced else "TOKEN-1"


@pytest.fixture
async def client():
    async with aiohttp.ClientSession() as s:
        yield CkwClient(s, FakeTokens())


def req(m, method, url):
    return m.requests[(method, __import__("yarl").URL(url))]


async def test_contracts_and_gpnr(client, fx):
    with aioresponses() as m:
        m.get(CONTRACTS, body=fx("api_contracts.json"))
        m.get(ACCOUNT, body=fx("api_account.json"))
        cs = await client.contracts()
        assert [(c.location_id, c.meter_group_type) for c in cs] == [("1000001", "BASIC")]
        assert cs[0].supply_start == date(2021, 8, 1)
        assert await client.gpnr() == "Example"
        assert await client.gpnr() == "Example"  # cached
        assert len(req(m, "GET", ACCOUNT)) == 1
        assert req(m, "GET", CONTRACTS)[0].kwargs["headers"]["Authorization"] == "Bearer TOKEN-1"
        assert req(m, "GET", CONTRACTS)[0].kwargs["headers"]["x-ckw-gpnr"] == "Example"  # live API answers 400 without it
        assert "x-ckw-gpnr" not in req(m, "GET", ACCOUNT)[0].kwargs["headers"]


async def test_measurements_request_and_result(client, fx):
    with aioresponses() as m:
        m.get(ACCOUNT, body=fx("api_account.json"))
        m.post(MEAS, body=fx("api_hourly_2026-11-07.json"))
        ms, peaks = await client.measurements(CONTRACT, date(2026, 11, 7), date(2026, 11, 7))
        call = req(m, "POST", MEAS)[0]
        assert call.kwargs["json"] == {
            "locationId": "1000001", "activeMeterGroupType": "BASIC", "aggregation": "HOUR",
            "startDate": "2026-11-07", "endDate": "2026-11-07",
        }
        assert call.kwargs["headers"]["x-ckw-gpnr"] == "Example"
        assert len(ms) == 24 and peaks[0].kw == 1.68


async def test_quarter_hours_csv(client, fx):
    with aioresponses() as m:
        m.get(ACCOUNT, body=fx("api_account.json"))
        m.post(CSV, body=fx("quarter_hour_2026-11.csv").encode("utf-8"))
        rows = await client.quarter_hours(CONTRACT, date(2026, 11, 1), date(2026, 11, 30))
        assert len(rows) == 672
        assert req(m, "POST", CSV)[0].kwargs["json"]["aggregation"] == "FIFTEEN_MINUTES"


async def test_401_refreshes_token_once_then_succeeds(client, fx):
    with aioresponses() as m:
        m.get(ACCOUNT, body=fx("api_account.json"))
        m.get(CONTRACTS, status=401)
        m.get(CONTRACTS, body=fx("api_contracts.json"))
        assert len(await client.contracts()) == 1
        assert [c.kwargs["headers"]["Authorization"] for c in req(m, "GET", CONTRACTS)] == ["Bearer TOKEN-1", "Bearer TOKEN-2"]


async def test_persistent_401_requires_reauth(client, fx):
    with aioresponses() as m:
        m.get(ACCOUNT, body=fx("api_account.json"))
        m.get(CONTRACTS, status=401, repeat=True)
        with pytest.raises(ReauthRequired):
            await client.contracts()


async def test_server_error_and_bad_payloads(client, fx):
    with aioresponses() as m:
        m.get(ACCOUNT, body=fx("api_account.json"))
        m.get(CONTRACTS, status=503)
        with pytest.raises(CannotConnect):
            await client.contracts()
        m.get(CONTRACTS, body="<html>not json</html>")
        with pytest.raises(ParseError):
            await client.contracts()
        m.get(CONTRACTS, body=json.dumps({"body": {}}))
        with pytest.raises(ParseError):
            await client.contracts()
