"""Climate platform for Power Heating Controller."""

from __future__ import annotations

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
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
    async_add_entities(
        RoomThermostatClimate(coordinator, entry, room_id)
        for room_id in coordinator.room_configs
    )


class RoomThermostatClimate(CoordinatorEntity, ClimateEntity):
    """Climate entity for one room managed by the Power Heating Controller."""

    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )
    _attr_hvac_modes = [HVACMode.HEAT, HVACMode.OFF]
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 0.5
    _attr_min_temp = 5.0
    _attr_max_temp = 35.0
    _attr_icon = "mdi:radiator"

    def __init__(
        self,
        coordinator: PowerHeatingCoordinator,
        entry: ConfigEntry,
        room_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._room_id = room_id
        self._entry = entry
        cfg = coordinator.room_configs[room_id]
        self._attr_name = cfg.room_name
        self._attr_unique_id = f"{entry.entry_id}_{room_id}_climate"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._entry.entry_id)},
            "name": "Power Heating Controller",
            "manufacturer": "HAPHC",
            "model": "Power Heating Controller",
            "sw_version": "1.0.0",
        }

    @property
    def current_temperature(self) -> float | None:
        cfg = self.coordinator.room_configs.get(self._room_id)
        if cfg is None:
            return None
        state = self.hass.states.get(cfg.temp_sensor)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return None

    @property
    def target_temperature(self) -> float | None:
        cfg = self.coordinator.room_configs.get(self._room_id)
        return cfg.target_temp if cfg else None

    async def async_set_temperature(self, **kwargs) -> None:
        temp = kwargs.get(ATTR_TEMPERATURE)
        if temp is not None:
            await self.coordinator.async_set_target_temp(self._room_id, float(temp))

    @property
    def hvac_mode(self) -> HVACMode:
        cfg = self.coordinator.room_configs.get(self._room_id)
        if cfg is None or not cfg.enabled:
            return HVACMode.OFF
        return HVACMode.HEAT

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        enabled = hvac_mode == HVACMode.HEAT
        await self.coordinator.async_set_room_enabled(self._room_id, enabled)

    async def async_turn_on(self) -> None:
        await self.coordinator.async_set_room_enabled(self._room_id, True)

    async def async_turn_off(self) -> None:
        await self.coordinator.async_set_room_enabled(self._room_id, False)

    @property
    def hvac_action(self) -> HVACAction:
        cfg = self.coordinator.room_configs.get(self._room_id)
        if cfg is None or not cfg.enabled:
            return HVACAction.OFF
        state = self.coordinator.room_states.get(self._room_id)
        if state is None:
            return HVACAction.OFF
        if state.demand and state.permission:
            return HVACAction.HEATING
        return HVACAction.IDLE

    @property
    def extra_state_attributes(self) -> dict:
        cfg = self.coordinator.room_configs.get(self._room_id)
        state = self.coordinator.room_states.get(self._room_id)
        if cfg is None or state is None:
            return {}
        return {
            "demand": state.demand,
            "permission": state.permission,
            "waiting_for_power": state.demand and not state.permission,
            "thermal_deficit_c": round(state.deficit, 2),
            "priority": cfg.priority,
            "rated_power_w": cfg.power_w,
            "heater_entity": cfg.heater_entity,
            "temp_sensor": cfg.temp_sensor,
        }