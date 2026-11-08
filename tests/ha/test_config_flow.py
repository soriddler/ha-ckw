from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

from custom_components.ckw.ckw_client import CannotConnect, Contract, InvalidAuth, InvalidCode, Tokens
from custom_components.ckw.const import DOMAIN

from .conftest import LOC

TOKENS = Tokens("AT", "RT-1", 9e12)


class FakeLogin:
    def __init__(self, session=None):
        self.start_error = None
        self.code_errors = []
        self.resends = 0
        self.codes = []

    async def start(self, username, password):
        self.username, self.password = username, password
        if self.start_error:
            raise self.start_error

    async def resend_code(self):
        self.resends += 1

    async def submit_code(self, code):
        self.codes.append(code)
        if self.code_errors:
            raise self.code_errors.pop(0)
        return TOKENS


class FakeClient:
    contracts_result = [Contract(LOC, "CH0000", "BASIC", "Musterstrasse 1")]

    def __init__(self, session, tokens):
        pass

    async def contracts(self):
        return type(self).contracts_result


@pytest.fixture
def login():
    fake = FakeLogin()
    with patch("custom_components.ckw.config_flow.CkwLogin", return_value=fake), patch(
        "custom_components.ckw.config_flow.CkwClient", FakeClient
    ), patch("custom_components.ckw.async_setup_entry", return_value=True):
        FakeClient.contracts_result = [Contract(LOC, "CH0000", "BASIC", "Musterstrasse 1")]
        yield fake


async def start(hass):
    r = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert r["type"] is FlowResultType.FORM and r["step_id"] == "user"
    return r


async def test_happy_path_creates_entry(hass, login):
    r = await start(hass)
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"username": " me@x.ch ", "password": "pw"})
    assert r["step_id"] == "code" and login.username == "me@x.ch"
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"code": "123456"})
    assert r["type"] is FlowResultType.FORM and r["step_id"] == "history"
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"history_months": "36"})
    assert r["type"] is FlowResultType.CREATE_ENTRY
    assert r["title"] == "Musterstrasse 1"
    assert r["data"] == {
        "username": "me@x.ch", "password": "pw", "refresh_token": "RT-1",
        "location_id": LOC, "meter_group_type": "BASIC", "meter_id": "CH0000", "history_months": 36,
    }
    assert r["result"].unique_id == LOC


@pytest.mark.parametrize(
    ("error", "key"), [(InvalidAuth("bad"), "invalid_auth"), (CannotConnect("down"), "cannot_connect")]
)
async def test_password_step_errors(hass, login, error, key):
    login.start_error = error
    r = await start(hass)
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"username": "u", "password": "p"})
    assert r["type"] is FlowResultType.FORM and r["errors"] == {"base": key}
    login.start_error = None
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"username": "u", "password": "p"})
    assert r["step_id"] == "code"


async def test_wrong_code_then_correct_code(hass, login):
    login.code_errors = [InvalidCode("nope")]
    r = await start(hass)
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"username": "u", "password": "p"})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"code": "000000"})
    assert r["step_id"] == "code" and r["errors"] == {"code": "invalid_code"}
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"code": "123456"})
    assert r["step_id"] == "history"
    assert login.codes == ["000000", "123456"]


async def test_resend_and_empty_code(hass, login):
    r = await start(hass)
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"username": "u", "password": "p"})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"code": "", "resend": False})
    assert r["errors"] == {"code": "code_required"} and login.resends == 0
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"code": "", "resend": True})
    assert r["step_id"] == "code" and not r.get("errors")
    assert login.resends == 1 and r["description_placeholders"] == {"resent": "yes"}
    assert login.codes == []  # a resend never submits a code


async def test_multiple_contracts_ask_which(hass, login):
    FakeClient.contracts_result = [
        Contract(LOC, "CH0000", "BASIC", "Wohnung"),
        Contract("999", "CH1111", "BASIC", "Keller"),
    ]
    r = await start(hass)
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"username": "u", "password": "p"})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"code": "1"})
    assert r["step_id"] == "contract"
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"location_id": "999"})
    assert r["step_id"] == "history"
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"history_months": "12"})
    assert r["type"] is FlowResultType.CREATE_ENTRY and r["data"]["location_id"] == "999"


async def test_no_contracts_aborts(hass, login):
    FakeClient.contracts_result = []
    r = await start(hass)
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"username": "u", "password": "p"})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"code": "1"})
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "no_contracts"


async def test_duplicate_contract_aborts(hass, login, entry):
    r = await start(hass)
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"username": "u", "password": "p"})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"code": "1"})
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "already_configured"


async def test_reauth_updates_token_and_password(hass, login, entry):
    entry.async_start_reauth(hass)
    await hass.async_block_till_done()
    flow = next(iter(hass.config_entries.flow.async_progress_by_handler(DOMAIN)))
    assert flow["step_id"] == "reauth_confirm"
    r = await hass.config_entries.flow.async_configure(flow["flow_id"], {"password": "new-pw"})
    assert r["step_id"] == "code" and login.username == "user@example.org"
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"code": "123456"})
    assert r["type"] is FlowResultType.ABORT and r["reason"] == "reauth_successful"
    assert entry.data["refresh_token"] == "RT-1" and entry.data["password"] == "new-pw"
    assert entry.data["location_id"] == LOC  # rest untouched


def test_history_options_show_expected_duration():
    from datetime import date

    from custom_components.ckw.config_flow import history_options

    opts = history_options(date(2021, 8, 1), date(2026, 11, 8), "en")
    assert list(opts) == ["1", "3", "12", "36", "0"]
    assert opts["1"] == "Last 1 month (about 9 s)"
    assert opts["12"] == "Last 12 months (about 2 min)"
    assert opts["0"] == "Everything since contract start 2021-08 (64 months, about 10 min)"
    assert history_options(date(2021, 8, 1), date(2026, 11, 8), "fr")["12"] == "Derniers 12 mois (env. 2 min)"
    assert history_options(date(2021, 8, 1), date(2026, 11, 8), "fr")["1"] == "Derniers 1 mois (env. 9 s)"
    assert history_options(date(2021, 8, 1), date(2026, 11, 8), "de")["0"].startswith("Alles seit Vertragsbeginn 08.2021 (64 Monate, ca. 10 min)")


def test_history_options_hide_choices_longer_than_the_contract():
    from datetime import date

    from custom_components.ckw.config_flow import history_options

    opts = history_options(date(2026, 6, 20), date(2026, 11, 8), "en")  # 6 months of contract
    assert list(opts) == ["1", "3", "0"]
    unknown = history_options(None, date(2026, 11, 8), "en")  # contract start not reported
    assert list(unknown) == ["1", "3", "12", "36", "0"] and "several minutes" in unknown["0"]


async def test_history_step_offers_contract_specific_choices(hass, login):
    from datetime import date

    FakeClient.contracts_result = [Contract(LOC, "CH0000", "BASIC", "Musterstrasse 1", date(2021, 8, 1))]
    r = await start(hass)
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"username": "u", "password": "p"})
    r = await hass.config_entries.flow.async_configure(r["flow_id"], {"code": "1"})
    assert r["step_id"] == "history"
    choices = list(r["data_schema"].schema["history_months"].container)
    assert choices == ["1", "3", "12", "36", "0"]
