"""Climate platform for Power Heating Controller.

FIX (this revision):
  - RoomThermostatClimate now inherits from RestoreEntity so the enabled/off
    state survives HA restarts. Without this, every restart turned all rooms ON.
  - async_added_to_hass restores the last hvac_mode from HA storage.
  - Target temperature is only a local setpoint for the coordinator's thermostat
    algorithm; it is NOT pushed to the underlying hardware climate entity.
"""

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
from homeassistant.helpers.restore_state import RestoreEntity
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


class RoomThermostatClimate(CoordinatorEntity, ClimateEntity, RestoreEntity):
    """Climate entity for one room managed by the Power Heating Controller.

    This entity acts as a virtual thermostat — it does NOT directly represent
    the hardware heater. It gives the user a standard thermostat card to:
      - Set the target temperature for the coordinator's software thermostat
      - Enable or disable the room (HVACMode.HEAT / HVACMode.OFF)

    The coordinator drives the actual hardware heater independently.

    State persistence: the enabled flag is restored from HA's last-known state
    on restart, so rooms do not default to ON after every HA restart.
    """

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

    async def async_added_to_hass(self) -> None:
        """Restore last hvac_mode (enabled state) from HA storage on restart.

        This prevents rooms from defaulting to ON after every HA restart.
        If there is no previous state (first boot), we respect the value
        already stored in the coordinator config (cfg.enabled).
        """
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()
        if last_state is not None:
            # Restore enabled from the persisted hvac_mode
            was_enabled = last_state.state == HVACMode.HEAT
            cfg = self.coordinator.room_configs.get(self._room_id)
            if cfg is not None and cfg.enabled != was_enabled:
                # Update coordinator silently (no refresh) — the coordinator's
                # periodic poll will pick it up on the next cycle.
                cfg.enabled = was_enabled
                if not was_enabled:
                    state = self.coordinator.room_states.get(self._room_id)
                    if state:
                        state.demand = False
                        state.permission = False
                    # Fire-and-forget: turn hardware off without blocking setup
                    self.hass.async_create_task(
                        self.coordinator._apply_heater(self._room_id, active=False)
                    )

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
        """Update the coordinator's software thermostat setpoint.

        NOTE: This does NOT push a temperature to the hardware climate entity.
        The hardware thermostat keeps its own setpoint. HA only controls
        the hvac_mode (heat/off) to grant or revoke heating permission.
        """
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
        if state.demand and not state.permission:
            # Waiting for power budget — show as IDLE (no dedicated "waiting" action)
            return HVACAction.IDLE
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
