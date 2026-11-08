import json
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant

from custom_components.ckw.ckw_client import (
    Contract,
    parse_measurements,
    parse_monthly_peaks,
    parse_quarter_hour_csv,
)
from custom_components.ckw.const import DOMAIN
from pytest_homeassistant_custom_component.common import MockConfigEntry

FIX = Path(__file__).parent.parent / "fixtures"
LOC = "1000001"


@pytest.fixture(autouse=True)
def _enable(recorder_mock, enable_custom_integrations):
    # recorder_mock must be requested before anything that creates `hass`
    yield


@pytest.fixture
def quarters():
    return parse_quarter_hour_csv((FIX / "quarter_hour_2026-11.csv").read_text(encoding="utf-8"))


class FakeClient:
    """Stands in for CkwClient; serves the recorded fixtures for the requested date range."""

    def __init__(self, quarters):
        self.quarters = quarters
        self.hourly = json.loads((FIX / "api_hourly_2026-11-07.json").read_text())
        self.calls = []
        self.error = None
        self.supply_start = None

    async def contracts(self):
        return [Contract(LOC, "CH0000", "BASIC", "Musterstrasse 1", self.supply_start)]

    async def quarter_hours(self, contract, start: date, end: date):
        self.calls.append(("csv", start, end))
        if self.error:
            raise self.error
        from zoneinfo import ZoneInfo

        tz = ZoneInfo("Europe/Zurich")
        return [q for q in self.quarters if start <= q.start.astimezone(tz).date() <= end]

    async def measurements(self, contract, start, end, aggregation="HOUR"):
        self.calls.append(("json", start, end))
        if self.error:
            raise self.error
        if start <= date(2026, 11, 7) <= end:
            return parse_measurements(self.hourly), parse_monthly_peaks(self.hourly)
        return [], []


@pytest.fixture
def fake_client(quarters):
    return FakeClient(quarters)


@pytest.fixture
def entry(hass: HomeAssistant):
    e = MockConfigEntry(
        domain=DOMAIN,
        title="Musterstrasse 1",
        unique_id=LOC,
        data={
            "username": "user@example.org",
            "password": "pw",
            "refresh_token": "RT-0",
            "location_id": LOC,
            "meter_group_type": "BASIC",
            "meter_id": "CH0000",
        },
    )
    e.add_to_hass(hass)
    return e


@pytest.fixture
def patched_client(fake_client):
    with patch("custom_components.ckw.CkwClient", return_value=fake_client):
        yield fake_client
