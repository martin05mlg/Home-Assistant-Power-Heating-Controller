"""Climate platform for Power Heating Controller.

One virtual thermostat per room. It does NOT represent the hardware heater:
  - the target temperature feeds the coordinator's software thermostat;
  - HEAT / OFF enables or disables the room.
Both values are persisted in the config entry by the coordinator.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import MAX_TARGET_TEMP, MIN_TARGET_TEMP, TARGET_TEMP_STEP
from .coordinator import PowerHeatingConfigEntry
from .entity import PowerHeatingRoomEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PowerHeatingConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one climate entity per room."""
    coordinator = entry.runtime_data
    async_add_entities(
        RoomThermostatClimate(coordinator, room_id) for room_id in coordinator.room_configs
    )


class RoomThermostatClimate(PowerHeatingRoomEntity, ClimateEntity):
    """Virtual thermostat for one room."""

    _attr_name = None  # Use the room (device) name.
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF]
    _attr_native_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = TARGET_TEMP_STEP
    _attr_min_temp = MIN_TARGET_TEMP
    _attr_max_temp = MAX_TARGET_TEMP

    def __init__(self, coordinator, room_id: str) -> None:
        super().__init__(coordinator, room_id, "climate")

    @property
    def native_current_temperature(self) -> float | None:
        state = self.coordinator.room_states.get(self._room_id)
        return state.current_temp if state else None

    @property
    def native_target_temperature(self) -> float | None:
        cfg = self.coordinator.room_configs.get(self._room_id)
        return cfg.target_temp if cfg else None

    # Home Assistant before 2026.11 still reads the original climate properties.
    if not hasattr(ClimateEntity, "native_temperature_unit"):
        _attr_temperature_unit = _attr_native_temperature_unit
        current_temperature = native_current_temperature
        target_temperature = native_target_temperature

    @property
    def hvac_mode(self) -> HVACMode:
        cfg = self.coordinator.room_configs.get(self._room_id)
        return HVACMode.HEAT if cfg and cfg.enabled else HVACMode.OFF

    @property
    def hvac_action(self) -> HVACAction:
        cfg = self.coordinator.room_configs.get(self._room_id)
        state = self.coordinator.room_states.get(self._room_id)
        if cfg is None or state is None or not cfg.enabled:
            return HVACAction.OFF
        return HVACAction.HEATING if state.permission else HVACAction.IDLE

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Update the software thermostat setpoint (not pushed to the hardware)."""
        if (hvac_mode := kwargs.get(ATTR_HVAC_MODE)) is not None:
            await self.async_set_hvac_mode(hvac_mode)
        if (temp := kwargs.get(ATTR_TEMPERATURE)) is not None:
            await self.coordinator.async_set_target_temp(self._room_id, float(temp))

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        await self.coordinator.async_set_room_enabled(
            self._room_id, hvac_mode == HVACMode.HEAT
        )

    async def async_turn_on(self) -> None:
        await self.coordinator.async_set_room_enabled(self._room_id, True)

    async def async_turn_off(self) -> None:
        await self.coordinator.async_set_room_enabled(self._room_id, False)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        cfg = self.coordinator.room_configs.get(self._room_id)
        state = self.coordinator.room_states.get(self._room_id)
        if cfg is None or state is None:
            return {}
        return {
            "room_id": self._room_id,
            "status": self.coordinator.room_status(self._room_id),
            "demand": state.demand,
            "permission": state.permission,
            "waiting_for_power": cfg.enabled and state.demand and not state.permission,
            "thermal_deficit_c": round(state.deficit, 2),
            "priority": cfg.priority,
            "rated_power_w": cfg.power_w,
            "heater_entity": cfg.heater_entity,
            "temp_sensor": cfg.temp_sensor,
        }
