"""Number platform for Power Heating Controller — global max power limit."""

from __future__ import annotations

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberMode
from homeassistant.const import EntityCategory, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import MAX_MAX_POWER, MIN_MAX_POWER
from .coordinator import PowerHeatingConfigEntry
from .entity import PowerHeatingEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PowerHeatingConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the max power number."""
    async_add_entities([MaxPowerNumber(entry.runtime_data, "max_power")])


class MaxPowerNumber(PowerHeatingEntity, NumberEntity):
    """Contracted power limit (persisted by the coordinator)."""

    _attr_translation_key = "max_power"
    _attr_device_class = NumberDeviceClass.POWER
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_native_min_value = MIN_MAX_POWER
    _attr_native_max_value = MAX_MAX_POWER
    _attr_native_step = 100
    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def native_value(self) -> float:
        return self.coordinator.max_power

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.async_set_max_power(value)
