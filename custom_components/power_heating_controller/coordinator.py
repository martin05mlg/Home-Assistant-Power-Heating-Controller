"""
Coordinator for Power Heating Controller.

This is the brain of the integration. It:
  1. Tracks current power consumption via a sensor.
  2. Monitors each room's temperature vs setpoint (demand).
  3. Grants or revokes heating permission per room to stay within the power budget.
  4. Applies a preemption algorithm: the coldest-relative-to-setpoint room
     can displace the room with the smallest thermal deficit.
  5. Drives the actual heater (climate entity or switch) through HA service calls.
  6. Restores the last known enabled/permission state across HA restarts.

All demand/permission state is kept internally — no input_booleans are created.

FIX NOTES (this revision):
  - Heater target temperature is no longer pushed to the climate entity (Tuya/etc.
    thermostats have their own setpoint; we only control ON/OFF via hvac_mode).
    When activating a climate heater, we set it to hvac_mode_heat only.
    The user controls the hardware thermostat setpoint separately.
  - Bootstrap timeout: _register_listeners and async_setup no longer await
    potentially-slow tasks; everything is non-blocking.
  - Room enabled state is stored in HA's storage (RestoreEntity pattern is used
    in climate.py) so rooms keep their last on/off state across restarts.
  - async_remove_room now returns the list of entity unique_ids that should be
    cleaned from the entity registry, so __init__.py can remove them.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    COORDINATOR_UPDATE_INTERVAL,
    DEFAULT_COLD_TOLERANCE,
    DEFAULT_HOT_TOLERANCE,
    DOMAIN,
    HEATER_TYPE_CLIMATE,
    HEATER_TYPE_SWITCH,
    OVERPOWER_GRACE_SECONDS,
    POWER_OK_GRACE_SECONDS,
    STATE_DEMAND,
    STATE_PERMISSION,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class RoomConfig:
    """Static configuration for one room."""
    room_id: str
    room_name: str
    heater_type: str
    heater_entity: str
    temp_sensor: str
    target_temp: float
    power_w: int
    priority: int
    hvac_mode_heat: str = "heat"
    enabled: bool = True


@dataclass
class RoomState:
    """Dynamic runtime state for one room (internal)."""
    demand: bool = False
    permission: bool = False
    deficit: float = 0.0

    def __repr__(self) -> str:
        return (
            f"RoomState(demand={self.demand}, permission={self.permission}, "
            f"deficit={self.deficit:.2f}°C)"
        )


class PowerHeatingCoordinator(DataUpdateCoordinator):
    """Manages the power-aware heating logic for all rooms."""

    def __init__(
        self,
        hass: HomeAssistant,
        power_sensor: str,
        max_power: float,
        rooms: list[RoomConfig],
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=COORDINATOR_UPDATE_INTERVAL),
        )
        self.power_sensor = power_sensor
        self.max_power = max_power
        self.room_configs: dict[str, RoomConfig] = {r.room_id: r for r in rooms}
        self.room_states: dict[str, RoomState] = {
            r.room_id: RoomState() for r in rooms
        }
        self._overpower_task: asyncio.Task | None = None
        self._power_ok_task: asyncio.Task | None = None
        self._unsub_listeners: list[Any] = []

    async def async_setup(self) -> None:
        """Register state listeners. Non-blocking — does not await slow I/O."""
        self._register_listeners_sync()

    async def async_unload(self) -> None:
        for unsub in self._unsub_listeners:
            unsub()
        self._unsub_listeners.clear()

        # Cancel any pending grace-period tasks
        for task in (self._overpower_task, self._power_ok_task):
            if task and not task.done():
                task.cancel()

    async def _async_update_data(self) -> dict:
        await self._update_all_demands()
        await self._rebalance_permissions()
        return self._build_snapshot()

    def get_available_power(self) -> float:
        current = self._get_float_state(self.power_sensor)
        return self.max_power - (current or 0.0)

    def get_room_state(self, room_id: str) -> RoomState | None:
        return self.room_states.get(room_id)

    def get_room_config(self, room_id: str) -> RoomConfig | None:
        return self.room_configs.get(room_id)

    def list_rooms(self) -> list[RoomConfig]:
        return list(self.room_configs.values())

    async def async_add_room(self, config: RoomConfig) -> None:
        self.room_configs[config.room_id] = config
        self.room_states[config.room_id] = RoomState()
        self._register_listeners_sync()
        await self.async_refresh()

    async def async_remove_room(self, room_id: str) -> list[str]:
        """Remove a room and return entity unique_id suffixes for registry cleanup."""
        removed_unique_ids: list[str] = []
        if room_id in self.room_configs:
            await self._apply_heater(room_id, active=False)
            # Collect unique_id fragments so caller can clean the entity registry
            removed_unique_ids = [
                f"{room_id}_climate",
                f"{room_id}_status",
            ]
            del self.room_configs[room_id]
            del self.room_states[room_id]
        self._register_listeners_sync()
        await self.async_refresh()
        return removed_unique_ids

    async def async_set_target_temp(self, room_id: str, temp: float) -> None:
        if cfg := self.room_configs.get(room_id):
            cfg.target_temp = temp
            # If the room is currently heating, re-apply (no setpoint push — see header note)
            state = self.room_states.get(room_id)
            if state and state.permission:
                await self._apply_heater(room_id, active=True)
        await self.async_refresh()

    async def async_set_max_power(self, watts: float) -> None:
        self.max_power = watts
        await self.async_refresh()

    async def async_set_room_enabled(self, room_id: str, enabled: bool) -> None:
        if cfg := self.room_configs.get(room_id):
            cfg.enabled = enabled
            if not enabled:
                state = self.room_states.get(room_id)
                if state:
                    state.demand = False
                    state.permission = False
                await self._apply_heater(room_id, active=False)
        await self.async_refresh()

    def _register_listeners_sync(self) -> None:
        """Re-register state-change listeners. Fully synchronous, no awaiting."""
        for unsub in self._unsub_listeners:
            unsub()
        self._unsub_listeners.clear()

        entities_to_watch = [self.power_sensor] + [
            cfg.temp_sensor
            for cfg in self.room_configs.values()
            if cfg.enabled
        ]

        @callback
        def _on_state_change(event):
            entity_id = event.data.get("entity_id")
            new_state = event.data.get("new_state")
            if new_state is None:
                return

            if entity_id == self.power_sensor:
                available = self.get_available_power()
                if available < 0:
                    self._schedule_overpower_action()
                elif available > 1000:
                    self._schedule_power_ok_action()
                return

            for room_id, cfg in self.room_configs.items():
                if cfg.temp_sensor == entity_id and cfg.enabled:
                    self.hass.async_create_task(
                        self._update_single_demand(room_id)
                    )
                    break

        self._unsub_listeners.append(
            async_track_state_change_event(
                self.hass, entities_to_watch, _on_state_change
            )
        )

    # Keep async_setup backward-compat alias
    async def _register_listeners(self) -> None:
        self._register_listeners_sync()

    async def _update_all_demands(self) -> None:
        for room_id in list(self.room_configs.keys()):
            await self._update_single_demand(room_id)

    async def _update_single_demand(self, room_id: str) -> None:
        cfg = self.room_configs.get(room_id)
        state = self.room_states.get(room_id)
        if not cfg or not state or not cfg.enabled:
            return

        current_temp = self._get_float_state(cfg.temp_sensor)
        if current_temp is None:
            return

        deficit = cfg.target_temp - current_temp
        state.deficit = deficit
        old_demand = state.demand

        if current_temp < (cfg.target_temp - DEFAULT_COLD_TOLERANCE):
            state.demand = True
        elif current_temp > (cfg.target_temp + DEFAULT_HOT_TOLERANCE):
            state.demand = False
            if state.permission:
                state.permission = False
                await self._apply_heater(room_id, active=False)

        if state.demand != old_demand:
            _LOGGER.debug(
                "Room %s demand changed: %s → %s (deficit=%.2f°C)",
                room_id, old_demand, state.demand, deficit,
            )
            if state.demand:
                await self._try_grant_permission(room_id)

    async def _try_grant_permission(self, room_id: str) -> bool:
        cfg = self.room_configs.get(room_id)
        state = self.room_states.get(room_id)
        if not cfg or not state:
            return False
        if not cfg.enabled or not state.demand or state.permission:
            return False

        available = self.get_available_power()
        if available >= cfg.power_w:
            state.permission = True
            await self._apply_heater(room_id, active=True)
            _LOGGER.info(
                "Room %s: permission granted (available=%.0fW, needed=%dW)",
                room_id, available, cfg.power_w,
            )
            self.async_set_updated_data(self._build_snapshot())
            return True
        return False

    async def _rebalance_permissions(self) -> None:
        for room_id, state in self.room_states.items():
            if state.permission and not state.demand:
                state.permission = False
                await self._apply_heater(room_id, active=False)

        sorted_rooms = sorted(
            [(room_id, cfg) for room_id, cfg in self.room_configs.items() if cfg.enabled],
            key=lambda x: x[1].priority,
        )
        for room_id, _ in sorted_rooms:
            state = self.room_states.get(room_id)
            if state and state.demand and not state.permission:
                await self._try_grant_permission(room_id)

    def _schedule_overpower_action(self) -> None:
        if self._overpower_task and not self._overpower_task.done():
            self._overpower_task.cancel()
        self._overpower_task = self.hass.async_create_task(self._handle_overpower())

    async def _handle_overpower(self) -> None:
        await asyncio.sleep(OVERPOWER_GRACE_SECONDS)
        if self.get_available_power() >= 0:
            return
        _LOGGER.warning(
            "Over power budget by %.0fW — revoking least-urgent permission",
            -self.get_available_power(),
        )
        victim = self._find_victim()
        if victim:
            self.room_states[victim].permission = False
            await self._apply_heater(victim, active=False)
            _LOGGER.info("Room %s: permission revoked (overpower)", victim)
            self.async_set_updated_data(self._build_snapshot())

    def _find_victim(self) -> str | None:
        """Return the room_id of the least-urgent active room.

        Sort order: highest priority number first (least important room),
        then smallest thermal deficit among equals.
        """
        candidates = [
            (room_id, state.deficit, self.room_configs[room_id].priority)
            for room_id, state in self.room_states.items()
            if state.permission and self.room_configs[room_id].enabled
        ]
        if not candidates:
            return None
        # Highest priority number = least important; among ties, smallest deficit yields first
        candidates.sort(key=lambda x: (-x[2], x[1]))
        return candidates[0][0]

    def _schedule_power_ok_action(self) -> None:
        if self._power_ok_task and not self._power_ok_task.done():
            self._power_ok_task.cancel()
        self._power_ok_task = self.hass.async_create_task(self._handle_power_ok())

    async def _handle_power_ok(self) -> None:
        await asyncio.sleep(POWER_OK_GRACE_SECONDS)
        await self._rebalance_permissions()
        self.async_set_updated_data(self._build_snapshot())

    async def async_preempt_for_room(self, room_id: str) -> None:
        cfg = self.room_configs.get(room_id)
        state = self.room_states.get(room_id)
        if not cfg or not state or not state.demand or state.permission:
            return

        available = self.get_available_power()
        if available >= cfg.power_w:
            await self._try_grant_permission(room_id)
            return

        pool = [
            (rid, self.room_states[rid].deficit, self.room_configs[rid].priority, self.room_configs[rid].power_w)
            for rid in self.room_configs
            if self.room_states[rid].permission
            and self.room_configs[rid].priority > cfg.priority
            and rid != room_id
        ]
        if not pool:
            return

        pool.sort(key=lambda x: x[1])
        freed_w = 0
        for victim_id, _, _, victim_w in pool:
            self.room_states[victim_id].permission = False
            await self._apply_heater(victim_id, active=False)
            freed_w += victim_w
            _LOGGER.info("Room %s preempted by %s (freed %dW)", victim_id, room_id, victim_w)
            if freed_w + available >= cfg.power_w:
                break

        await asyncio.sleep(0.1)
        await self._try_grant_permission(room_id)
        self.async_set_updated_data(self._build_snapshot())

    async def _apply_heater(self, room_id: str, active: bool) -> None:
        cfg = self.room_configs.get(room_id)
        if not cfg:
            return
        if cfg.heater_type == HEATER_TYPE_CLIMATE:
            await self._apply_climate(cfg, active)
        elif cfg.heater_type == HEATER_TYPE_SWITCH:
            await self._apply_switch(cfg, active)

    async def _apply_climate(self, cfg: RoomConfig, active: bool) -> None:
        """
        Control a climate entity.

        FIX: We no longer push the target temperature to the hardware thermostat.
        Many thermostats (e.g. Tuya/Beok) have their own physical setpoint that
        the user controls; overwriting it from HA causes "Unrecognised HVAC Mode"
        warnings and interferes with the hardware dial.

        We only toggle hvac_mode: hvac_mode_heat to activate, 'off' to deactivate.
        The hardware thermostat itself decides when its element fires based on its
        own setpoint — HA's role is purely to grant/revoke permission via mode.
        """
        if active:
            await self.hass.services.async_call(
                "climate", "set_hvac_mode",
                {"entity_id": cfg.heater_entity, "hvac_mode": cfg.hvac_mode_heat},
                blocking=False,
            )
        else:
            await self.hass.services.async_call(
                "climate", "set_hvac_mode",
                {"entity_id": cfg.heater_entity, "hvac_mode": "off"},
                blocking=False,
            )

    async def _apply_switch(self, cfg: RoomConfig, active: bool) -> None:
        service = "turn_on" if active else "turn_off"
        await self.hass.services.async_call(
            "switch", service, {"entity_id": cfg.heater_entity}, blocking=False,
        )

    def _get_float_state(self, entity_id: str) -> float | None:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            return float(state.state)
        except (ValueError, TypeError):
            return None

    def _build_snapshot(self) -> dict:
        return {
            "available_power": self.get_available_power(),
            "max_power": self.max_power,
            "current_power": self._get_float_state(self.power_sensor) or 0.0,
            "rooms": {
                room_id: {
                    "name": cfg.room_name,
                    "enabled": cfg.enabled,
                    "demand": self.room_states[room_id].demand,
                    "permission": self.room_states[room_id].permission,
                    "deficit": self.room_states[room_id].deficit,
                    "target_temp": cfg.target_temp,
                    "power_w": cfg.power_w,
                    "priority": cfg.priority,
                }
                for room_id, cfg in self.room_configs.items()
            },
        }
