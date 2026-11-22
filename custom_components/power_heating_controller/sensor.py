"""Sensor platform for Power Heating Controller."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ROOM_STATUSES, WAIT_REASONS
from .coordinator import PowerHeatingConfigEntry
from .entity import PowerHeatingEntity, PowerHeatingRoomEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PowerHeatingConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up global power sensors and one status sensor per room."""
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        AvailablePowerSensor(coordinator, "available_power"),
        CurrentPowerSensor(coordinator, "current_power"),
    ]
    entities.extend(
        RoomStatusSensor(coordinator, room_id) for room_id in coordinator.room_configs
    )
    entities.extend(
        RoomWaitingReasonSensor(coordinator, room_id) for room_id in coordinator.room_configs
    )
    async_add_entities(entities)


class _PowerSensor(PowerHeatingEntity, SensorEntity):
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 0
    _data_key: str

    def __init__(self, coordinator, key: str) -> None:
        super().__init__(coordinator, key)
        self._attr_translation_key = key

    @property
    def native_value(self) -> float | None:
        if not self.coordinator.data:
            return None
        value = self.coordinator.data.get(self._data_key)
        return round(value, 1) if value is not None else None


class AvailablePowerSensor(_PowerSensor):
    """Watts that can still be granted before reaching the limit."""

    _data_key = "available_power"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self.coordinator.data:
            return {}
        return {
            "max_power_w": self.coordinator.data.get("max_power"),
            "current_power_w": self.coordinator.data.get("current_power"),
        }


class CurrentPowerSensor(_PowerSensor):
    """Household consumption as seen by the controller (always in W)."""

    _data_key = "current_power"


class RoomStatusSensor(PowerHeatingRoomEntity, SensorEntity):
    """heating / waiting / idle / disabled."""

    _attr_translation_key = "room_status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ROOM_STATUSES

    def __init__(self, coordinator, room_id: str) -> None:
        super().__init__(coordinator, room_id, "status")

    @property
    def native_value(self) -> str | None:
        return self.coordinator.room_status(self._room_id)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        cfg = self.coordinator.room_configs.get(self._room_id)
        state = self.coordinator.room_states.get(self._room_id)
        if cfg is None or state is None:
            return {}
        return {
            "demand": state.demand,
            "permission": state.permission,
            "deficit_c": round(state.deficit, 2),
            "current_temp_c": state.current_temp,
            "target_temp_c": cfg.target_temp,
            "priority": cfg.priority,
            "rated_power_w": cfg.power_w,
        }


class RoomWaitingReasonSensor(PowerHeatingRoomEntity, SensorEntity):
    """Explain why the room has no heating permission."""

    _attr_translation_key = "waiting_reason"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = WAIT_REASONS

    def __init__(self, coordinator, room_id: str) -> None:
        super().__init__(coordinator, room_id, "waiting_reason")

    @property
    def native_value(self) -> str | None:
        data = self.coordinator.data
        if data is None:
            return None
        return data["rooms"].get(self._room_id, {}).get("waiting_reason")
