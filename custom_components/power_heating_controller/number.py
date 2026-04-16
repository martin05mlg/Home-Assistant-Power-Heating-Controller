"""Number platform for Power Heating Controller — global max power limit only."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import PowerHeatingCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: PowerHeatingCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([MaxPowerNumber(coordinator, entry)])


class MaxPowerNumber(CoordinatorEntity, NumberEntity):
    _attr_name = "Max Power Limit"
    _attr_native_unit_of_measurement = "W"
    _attr_native_min_value = 500
    _attr_native_max_value = 20000
    _attr_native_step = 100
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:transmission-tower"

    def __init__(self, coordinator: PowerHeatingCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.entry_id}_max_power"
        self._entry = entry

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Power Heating Controller",
        }

    @property
    def native_value(self) -> float:
        return self.coordinator.max_power

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_max_power(value)