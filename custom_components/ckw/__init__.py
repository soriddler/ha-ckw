"""CKW (Centralschweizerische Kraftwerke) smart-meter data for Home Assistant."""
from __future__ import annotations

from datetime import date, datetime

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_change

from .ckw_client import CkwClient, TokenManager
from .const import CONF_REFRESH_TOKEN, DOMAIN, UPDATE_HOUR, UPDATE_MINUTE
from .coordinator import CkwCoordinator

PLATFORMS = ["sensor"]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type CkwConfigEntry = ConfigEntry[CkwCoordinator]

SERVICE_IMPORT_HISTORY = "import_history"
IMPORT_HISTORY_SCHEMA = vol.Schema({vol.Optional("start_date"): cv.date})


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    async def import_history(call: ServiceCall) -> None:
        """Delete the imported CKW statistics and import everything again (runs in the background)."""
        start: date | None = call.data.get("start_date")
        entries = [
            e for e in hass.config_entries.async_entries(DOMAIN) if e.state is ConfigEntryState.LOADED
        ]
        for entry in entries:
            entry.async_create_background_task(
                hass, entry.runtime_data.async_reimport(start), f"ckw import history {entry.entry_id}"
            )

    hass.services.async_register(DOMAIN, SERVICE_IMPORT_HISTORY, import_history, schema=IMPORT_HISTORY_SCHEMA)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: CkwConfigEntry) -> bool:
    session = async_get_clientsession(hass)

    async def persist_refresh_token(token: str) -> None:
        # the refresh token rotates on every use: store the new one before anything else
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_REFRESH_TOKEN: token})

    tokens = TokenManager(session, entry.data[CONF_REFRESH_TOKEN], persist_refresh_token)
    coordinator = CkwCoordinator(hass, entry, CkwClient(session, tokens))
    entry.runtime_data = coordinator
    if await coordinator.has_history():
        # normal start: only the last days are fetched, quick enough to wait for
        try:
            await coordinator.async_config_entry_first_refresh()
        except BaseException:
            coordinator.cancel_retry()  # Home Assistant retries the whole setup itself
            raise
    else:
        # first setup: the 12-month import takes minutes; do not block the setup dialog.
        # Sensors stay unavailable until it is done; progress is logged.
        entry.async_create_background_task(hass, coordinator.async_refresh(), "ckw initial import")

    async def _daily(_now: datetime) -> None:
        await coordinator.async_refresh()

    entry.async_on_unload(async_track_time_change(hass, _daily, hour=UPDATE_HOUR, minute=UPDATE_MINUTE, second=0))
    entry.async_on_unload(coordinator.cancel_retry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: CkwConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
