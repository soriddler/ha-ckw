"""Sensors with the newest day's values and the running month's peak."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
)
from homeassistant.const import EntityCategory, UnitOfEnergy, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_LOCATION_ID, DOMAIN, MANUFACTURER
from .coordinator import CkwCoordinator, CkwData


@dataclass(frozen=True, kw_only=True)
class CkwSensorDescription(SensorEntityDescription):
    value: Callable[[CkwData], float | datetime | None]
    attrs: Callable[[CkwData], dict] | None = None


SENSORS: tuple[CkwSensorDescription, ...] = (
    CkwSensorDescription(
        key="day_energy",
        translation_key="day_energy",
        device_class=SensorDeviceClass.ENERGY,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=2,
        value=lambda d: d.day_energy_kwh,
        attrs=lambda d: {"date": d.day.isoformat() if d.day else None},
    ),
    CkwSensorDescription(
        key="day_peak",
        translation_key="day_peak",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        suggested_display_precision=2,
        value=lambda d: d.day_peak_kw,
        attrs=lambda d: {"date": d.day.isoformat() if d.day else None},
    ),
    CkwSensorDescription(
        key="day_cost",
        translation_key="day_cost",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="CHF",
        suggested_display_precision=2,
        value=lambda d: d.day_cost_chf,
        attrs=lambda d: {"date": d.day.isoformat() if d.day else None},
    ),
    CkwSensorDescription(
        key="month_peak",
        translation_key="month_peak",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.KILO_WATT,
        suggested_display_precision=2,
        value=lambda d: d.month_peak_kw,
        attrs=lambda d: {"at": d.month_peak_at.isoformat() if d.month_peak_at else None},
    ),
    CkwSensorDescription(
        key="month_power_charge",
        translation_key="month_power_charge",
        device_class=SensorDeviceClass.MONETARY,
        native_unit_of_measurement="CHF",
        suggested_display_precision=2,
        value=lambda d: d.month_power_charge_chf,
    ),
    CkwSensorDescription(
        key="login_expires",
        translation_key="login_expires",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda d: d.login_expires,
    ),
    CkwSensorDescription(
        key="last_data",
        translation_key="last_data",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda d: d.last_data,
    ),
)


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    coordinator: CkwCoordinator = entry.runtime_data
    async_add_entities(CkwSensor(coordinator, entry, d) for d in SENSORS)


class CkwSensor(CoordinatorEntity[CkwCoordinator], SensorEntity):
    _attr_has_entity_name = True
    entity_description: CkwSensorDescription

    def __init__(self, coordinator: CkwCoordinator, entry, description: CkwSensorDescription) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.data[CONF_LOCATION_ID]}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data[CONF_LOCATION_ID])},
            name=entry.title or "CKW",
            manufacturer=MANUFACTURER,
        )

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.data is not None

    @property
    def native_value(self):
        return self.entity_description.value(self.coordinator.data)

    @property
    def extra_state_attributes(self):
        attrs = self.entity_description.attrs
        return attrs(self.coordinator.data) if attrs else None
