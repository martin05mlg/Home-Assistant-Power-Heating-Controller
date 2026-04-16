"""Sensor platform for Power Heating Controller."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass, SensorEntity, SensorStateClass,
)
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
    entities = [
        AvailablePowerSensor(coordinator, entry),
        CurrentPowerSensor(coordinator, entry),
    ]
    for room_id in coordinator.room_configs:
        entities.append(RoomStatusSensor(coordinator, entry, room_id))
    async_add_entities(entities)


class _BaseCoordinatorSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator: PowerHeatingCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Power Heating Controller",
            "manufacturer": "HAPHC",
            "model": "Power Heating Controller",
            "sw_version": "1.0.0",
        }


class AvailablePowerSensor(_BaseCoordinatorSensor):
    _attr_name = "Available Power"
    _attr_native_unit_of_measurement = "W"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:lightning-bolt-circle"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_available_power"

    @property
    def native_value(self):
        if self.coordinator.data:
            return round(self.coordinator.data.get("available_power", 0), 1)
        return None

    @property
    def extra_state_attributes(self):
        if not self.coordinator.data:
            return {}
        return {
            "max_power_w": self.coordinator.data.get("max_power"),
            "current_power_w": self.coordinator.data.get("current_power"),
        }


class CurrentPowerSensor(_BaseCoordinatorSensor):
    _attr_name = "Current Power Consumption"
    _attr_native_unit_of_measurement = "W"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:home-lightning-bolt"

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_current_power"

    @property
    def native_value(self):
        if self.coordinator.data:
            return round(self.coordinator.data.get("current_power", 0), 1)
        return None


class RoomStatusSensor(_BaseCoordinatorSensor):
    _attr_icon = "mdi:thermometer"

    def __init__(self, coordinator, entry, room_id):
        super().__init__(coordinator, entry)
        self._room_id = room_id
        cfg = coordinator.room_configs[room_id]
        self._attr_name = f"{cfg.room_name} Heating Status"
        self._attr_unique_id = f"{entry.entry_id}_{room_id}_status"

    def _room_data(self):
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("rooms", {}).get(self._room_id)

    @property
    def native_value(self) -> str:
        rd = self._room_data()
        if rd is None:
            return "unavailable"
        if not rd.get("enabled"):
            return "disabled"
        if rd.get("demand") and rd.get("permission"):
            return "heating"
        if rd.get("demand") and not rd.get("permission"):
            return "waiting"
        return "idle"

    @property
    def extra_state_attributes(self):
        rd = self._room_data()
        if rd is None:
            return {}
        return {
            "demand": rd.get("demand"),
            "permission": rd.get("permission"),
            "deficit_c": round(rd.get("deficit", 0), 2),
            "target_temp_c": rd.get("target_temp"),
            "priority": rd.get("priority"),
            "rated_power_w": rd.get("power_w"),
        }