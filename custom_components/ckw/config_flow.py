"""Config flow: password, SMS code (with 'send new code'), contract choice, and re-login."""
from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import date
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .ckw_client import (
    CannotConnect,
    CkwClient,
    CkwError,
    CkwLogin,
    Contract,
    InvalidAuth,
    InvalidCode,
    Tokens,
    TokenManager,
)
from .const import (
    CONF_CODE,
    CONF_HISTORY_MONTHS,
    CONF_LOCATION_ID,
    CONF_METER_GROUP_TYPE,
    CONF_METER_ID,
    CONF_PASSWORD,
    CONF_REFRESH_TOKEN,
    CONF_RESEND,
    CONF_USERNAME,
    DOMAIN,
    HISTORY_CHOICES,
    SECONDS_PER_MONTH,
)

_LOGGER = logging.getLogger(__name__)


def months_between(start: date, end: date) -> int:
    return max(1, (end.year - start.year) * 12 + end.month - start.month + 1)


_TEXT = {
    "en": {"about": "about", "s": "s", "min": "min", "last": "Last", "month": "month", "months": "months",
           "all": "Everything since contract start", "several": "several minutes"},
    "de": {"about": "ca.", "s": "s", "min": "min", "last": "Letzte", "month": "Monat", "months": "Monate",
           "all": "Alles seit Vertragsbeginn", "several": "mehrere Minuten"},
    "fr": {"about": "env.", "s": "s", "min": "min", "last": "Derniers", "month": "mois", "months": "mois",
           "all": "Tout depuis le début du contrat", "several": "plusieurs minutes"},
}


def duration_text(months: int, lang: str = "en") -> str:
    t = _TEXT.get(lang, _TEXT["en"])
    seconds = months * SECONDS_PER_MONTH
    if seconds < 60:
        return f"{t['about']} {seconds} {t['s']}"
    return f"{t['about']} {max(1, round(seconds / 60))} {t['min']}"


def history_options(supply_start: date | None, today: date, language: str = "en") -> dict[str, str]:
    """{value: label} for the history choice, each with the expected duration of the first import."""
    lang = language[:2] if language[:2] in _TEXT else "en"
    t = _TEXT[lang]
    total = months_between(supply_start, today) if supply_start else None
    options: dict[str, str] = {}
    for m in HISTORY_CHOICES:
        if total is not None and m >= total:
            continue
        options[str(m)] = f"{t['last']} {m} {t['month'] if m == 1 else t['months']} ({duration_text(m, lang)})"
    if total is None:
        options["0"] = f"{t['all']} ({t['several']})"
    else:
        since = f"{supply_start:%Y-%m}" if lang == "en" else f"{supply_start:%m.%Y}"
        options["0"] = f"{t['all']} {since} ({total} {t['months']}, {duration_text(total, lang)})"
    return options


class CkwConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._session: aiohttp.ClientSession | None = None
        self._login: CkwLogin | None = None
        self._username = ""
        self._password = ""
        self._tokens: Tokens | None = None
        self._contracts: list[Contract] = []
        self._reauth = False
        self._resent = False
        self._chosen: Contract | None = None

    @callback
    def async_remove(self) -> None:
        if self._session is not None:
            self._session.detach()  # the session is not closed (Home Assistant owns it), just released

    # ------------------------------------------------------------------ helpers --
    async def _begin_login(self) -> None:
        """New login session (own cookie jar) and password step; the server then sends an SMS."""
        if self._session is not None:
            self._session.detach()
        self._session = async_create_clientsession(self.hass, auto_cleanup=False, cookie_jar=aiohttp.CookieJar())
        self._login = CkwLogin(self._session)
        self._resent = False
        await self._login.start(self._username, self._password)

    # ---------------------------------------------------------------------- steps --
    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._username = user_input[CONF_USERNAME].strip()
            self._password = user_input[CONF_PASSWORD]
            try:
                await self._begin_login()
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except CkwError:
                _LOGGER.exception("Unexpected login page")
                errors["base"] = "login_failed"
            else:
                return await self.async_step_code()
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_USERNAME): str, vol.Required(CONF_PASSWORD): str}),
            errors=errors,
        )

    async def async_step_code(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        assert self._login is not None
        if user_input is not None:
            try:
                if user_input.get(CONF_RESEND):
                    await self._login.resend_code()
                    self._resent = True
                elif user_input.get(CONF_CODE):
                    self._tokens = await self._login.submit_code(user_input[CONF_CODE])
                    return await self._login_done()
                else:
                    errors[CONF_CODE] = "code_required"
            except InvalidCode:
                errors[CONF_CODE] = "invalid_code"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except CkwError:
                _LOGGER.exception("Unexpected answer during the code step")
                errors["base"] = "login_failed"
        return self.async_show_form(
            step_id="code",
            data_schema=vol.Schema(
                {vol.Optional(CONF_CODE, default=""): str, vol.Optional(CONF_RESEND, default=False): bool}
            ),
            errors=errors,
            description_placeholders={"resent": "yes" if self._resent else "no"},
        )

    async def _login_done(self) -> ConfigFlowResult:
        assert self._tokens is not None and self._session is not None
        if self._reauth:
            entry = self._get_reauth_entry()
            return self.async_update_reload_and_abort(
                entry, data_updates={CONF_PASSWORD: self._password, CONF_REFRESH_TOKEN: self._tokens.refresh_token}
            )
        tm = TokenManager(self._session, self._tokens.refresh_token, self._remember_token, self._tokens)
        try:
            self._contracts = await CkwClient(self._session, tm).contracts()
        except CannotConnect:
            return self.async_abort(reason="cannot_connect")
        except CkwError:
            _LOGGER.exception("Could not read the contracts")
            return self.async_abort(reason="no_contracts")
        if not self._contracts:
            return self.async_abort(reason="no_contracts")
        if len(self._contracts) == 1:
            self._chosen = self._contracts[0]
            return await self.async_step_history()
        return await self.async_step_contract()

    async def _remember_token(self, token: str) -> None:
        assert self._tokens is not None
        self._tokens = Tokens(self._tokens.access_token, token, self._tokens.expires_at)

    async def async_step_contract(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._chosen = next(c for c in self._contracts if c.location_id == user_input[CONF_LOCATION_ID])
            return await self.async_step_history()
        return self.async_show_form(
            step_id="contract",
            data_schema=vol.Schema(
                {vol.Required(CONF_LOCATION_ID): vol.In({c.location_id: c.label for c in self._contracts})}
            ),
        )

    async def async_step_history(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """How much history to load first. Everything is possible but takes a few minutes."""
        assert self._chosen is not None
        await self.async_set_unique_id(self._chosen.location_id)
        self._abort_if_unique_id_configured()  # stop before asking for details
        if user_input is not None:
            return await self._create(self._chosen, int(user_input[CONF_HISTORY_MONTHS]))
        options = history_options(self._chosen.supply_start, date.today(), self.hass.config.language)
        default = "12" if "12" in options else next(iter(options))
        return self.async_show_form(
            step_id="history",
            data_schema=vol.Schema({vol.Required(CONF_HISTORY_MONTHS, default=default): vol.In(options)}),
        )

    async def _create(self, contract: Contract, history_months: int = 0) -> ConfigFlowResult:
        assert self._tokens is not None
        await self.async_set_unique_id(contract.location_id)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=contract.label or f"CKW {contract.meter_id}",
            data={
                CONF_USERNAME: self._username,
                CONF_PASSWORD: self._password,
                CONF_REFRESH_TOKEN: self._tokens.refresh_token,
                CONF_LOCATION_ID: contract.location_id,
                CONF_METER_GROUP_TYPE: contract.meter_group_type,
                CONF_METER_ID: contract.meter_id,
                CONF_HISTORY_MONTHS: history_months,
            },
        )

    # --------------------------------------------------------------------- reauth --
    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        self._reauth = True
        self._username = entry_data[CONF_USERNAME]
        self._password = entry_data.get(CONF_PASSWORD, "")
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._password = user_input[CONF_PASSWORD]
            try:
                await self._begin_login()  # sends a new SMS
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except CkwError:
                errors["base"] = "login_failed"
            else:
                return await self.async_step_code()
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD, default=self._password): str}),
            errors=errors,
            description_placeholders={"username": self._username},
        )
