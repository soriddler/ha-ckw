"""Diagnostics with credentials and personal data removed."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .statistics_import import COST, ENERGY, PEAK, statistic_id, summary
from .const import CONF_LOCATION_ID, CONF_METER_ID, CONF_PASSWORD, CONF_REFRESH_TOKEN, CONF_USERNAME

TO_REDACT = {CONF_USERNAME, CONF_PASSWORD, CONF_REFRESH_TOKEN, CONF_LOCATION_ID, CONF_METER_ID, "title", "unique_id"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry) -> dict[str, Any]:
    data = entry.runtime_data.data
    loc = entry.data[CONF_LOCATION_ID]
    stored = {kind: await summary(hass, statistic_id(kind, loc)) for kind in (ENERGY, COST, PEAK)}
    return {
        "stored_statistics": stored,
        "entry": async_redact_data({**entry.as_dict(), "data": dict(entry.data)}, TO_REDACT),
        "data": async_redact_data(asdict(data), set()) if data else None,
    }
