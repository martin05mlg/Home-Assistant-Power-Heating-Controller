"""Coordinator for Power Heating Controller.

This is the brain of the integration. It:
  1. Reads the household power consumption from a sensor (W, kW… are converted).
  2. Runs a software thermostat per room (demand ON/OFF with hysteresis).
  3. Grants or revokes heating permission per room so the total consumption
     stays within the contracted power limit.
  4. Preempts lower-priority (or much warmer same-priority) rooms when a more
     important room needs heat and there is not enough power.
  5. Sheds load when the measured consumption exceeds the limit.
  6. Drives the real heaters (climate or switch entities) and periodically
     reconciles their actual state with the desired one.

Design notes:
  * Every decision is taken inside ``self._lock`` so the periodic refresh,
    state listeners and the overpower task never race each other.
  * Runtime changes (target temperature, enabled flag, max power) are persisted
    into the config entry options, which is the single source of truth. The
    update listener in ``__init__`` ignores those runtime-only updates so they
    do not trigger a reload.
  * Fail-safe: no new permissions are granted while the power sensor is
    unavailable, and a room whose temperature sensor stays unavailable longer
    than ``SENSOR_UNAVAILABLE_TIMEOUT`` stops heating.
"""

from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
from datetime import timedelta
import logging
import time
from typing import Any

import voluptuous as vol

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ATTR_MAX_TEMP,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_SUPPORTED_FEATURES,
    ATTR_TEMPERATURE,
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfPower,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util.unit_conversion import PowerConverter

from .const import (
    CONF_COLD_TOLERANCE,
    CONF_ENABLED,
    CONF_FORCE_MAX_TEMP,
    CONF_HEATER_ENTITY,
    CONF_HEATER_TYPE,
    CONF_HOT_TOLERANCE,
    CONF_HVAC_MODE_HEAT,
    CONF_MAX_POWER,
    CONF_POWER_SENSOR,
    CONF_POWER_W,
    CONF_PRIORITY,
    CONF_ROOM_ID,
    CONF_ROOM_NAME,
    CONF_ROOMS,
    CONF_TARGET_TEMP,
    CONF_TEMP_SENSOR,
    COORDINATOR_UPDATE_INTERVAL,
    DEFAULT_COLD_TOLERANCE,
    DEFAULT_FORCE_MAX_TEMP,
    DEFAULT_HOT_TOLERANCE,
    DEFAULT_HVAC_MODE_HEAT,
    DEFAULT_MAX_POWER,
    DEFAULT_POWER_W,
    DEFAULT_PRIORITY,
    DEFAULT_TARGET_TEMP,
    DOMAIN,
    FALLBACK_HEATER_MAX_TEMP,
    HEATER_TYPE_CLIMATE,
    MIN_ON_TIME_SECONDS,
    OVERPOWER_GRACE_SECONDS,
    PENDING_POWER_SECONDS,
    POWER_OK_GRACE_SECONDS,
    PREEMPT_DEFICIT_MARGIN,
    RECONCILE_COOLDOWN_SECONDS,
    SENSOR_UNAVAILABLE_TIMEOUT,
    STATUS_DISABLED,
    STATUS_HEATING,
    STATUS_IDLE,
    STATUS_WAITING,
)

_LOGGER = logging.getLogger(__name__)

type PowerHeatingConfigEntry = ConfigEntry[PowerHeatingCoordinator]


@dataclass
class RoomConfig:
    """Configuration for one room."""

    room_id: str
    room_name: str
    heater_type: str
    heater_entity: str
    temp_sensor: str
    target_temp: float = DEFAULT_TARGET_TEMP
    power_w: int = DEFAULT_POWER_W
    priority: int = DEFAULT_PRIORITY
    hvac_mode_heat: str = DEFAULT_HVAC_MODE_HEAT
    force_max_temp: bool = DEFAULT_FORCE_MAX_TEMP
    enabled: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RoomConfig:
        """Build a RoomConfig from a config entry room dict."""
        return cls(
            room_id=data[CONF_ROOM_ID],
            room_name=data[CONF_ROOM_NAME],
            heater_type=data[CONF_HEATER_TYPE],
            heater_entity=data[CONF_HEATER_ENTITY],
            temp_sensor=data[CONF_TEMP_SENSOR],
            target_temp=float(data.get(CONF_TARGET_TEMP, DEFAULT_TARGET_TEMP)),
            power_w=int(data.get(CONF_POWER_W, DEFAULT_POWER_W)),
            priority=int(data.get(CONF_PRIORITY, DEFAULT_PRIORITY)),
            hvac_mode_heat=data.get(CONF_HVAC_MODE_HEAT) or DEFAULT_HVAC_MODE_HEAT,
            force_max_temp=bool(data.get(CONF_FORCE_MAX_TEMP, DEFAULT_FORCE_MAX_TEMP)),
            enabled=bool(data.get(CONF_ENABLED, True)),
        )


@dataclass
class RoomState:
    """Dynamic runtime state for one room (internal, not persisted)."""

    demand: bool = False
    permission: bool = False
    deficit: float = 0.0
    current_temp: float | None = None
    granted_at: float | None = None
    temp_unavailable_since: float | None = None
    last_command_at: float | None = None


class PowerHeatingCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Manages the power-aware heating logic for all rooms."""

    config_entry: PowerHeatingConfigEntry

    def __init__(self, hass: HomeAssistant, entry: PowerHeatingConfigEntry) -> None:
        """Initialise the coordinator from the config entry options."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=COORDINATOR_UPDATE_INTERVAL),
        )
        options = entry.options
        self.power_sensor: str = options[CONF_POWER_SENSOR]
        self.max_power: float = float(options.get(CONF_MAX_POWER, DEFAULT_MAX_POWER))
        self.cold_tolerance: float = float(
            options.get(CONF_COLD_TOLERANCE, DEFAULT_COLD_TOLERANCE)
        )
        self.hot_tolerance: float = float(
            options.get(CONF_HOT_TOLERANCE, DEFAULT_HOT_TOLERANCE)
        )
        self.room_configs: dict[str, RoomConfig] = {}
        for room in options.get(CONF_ROOMS, []):
            cfg = RoomConfig.from_dict(room)
            self.room_configs[cfg.room_id] = cfg
        self.room_states: dict[str, RoomState] = {
            room_id: RoomState() for room_id in self.room_configs
        }

        self._lock = asyncio.Lock()
        self._overpower_task: asyncio.Task | None = None
        self._shedding = False
        self._last_shed_at: float | None = None
        self._unsub_listeners: list[Any] = []
        self._expected_options: dict[str, Any] | None = None
        self._power_unavailable_logged = False

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def async_setup(self) -> None:
        """Register state listeners (non-blocking)."""
        entities = {self.power_sensor}
        entities.update(cfg.temp_sensor for cfg in self.room_configs.values())
        self._unsub_listeners.append(
            async_track_state_change_event(
                self.hass, sorted(entities), self._async_on_state_change
            )
        )

    async def async_unload(self) -> None:
        """Remove listeners and cancel pending tasks."""
        for unsub in self._unsub_listeners:
            unsub()
        self._unsub_listeners.clear()
        if self._overpower_task and not self._overpower_task.done():
            self._overpower_task.cancel()
        self._overpower_task = None

    async def _async_update_data(self) -> dict[str, Any]:
        async with self._lock:
            await self._async_control_cycle()
        return self._build_snapshot()

    # ── Public API ────────────────────────────────────────────────────────

    def has_room(self, room_id: str) -> bool:
        """Return True if the room exists."""
        return room_id in self.room_configs

    def get_current_power(self) -> float | None:
        """Return the measured household consumption in W (None if unknown)."""
        state = self.hass.states.get(self.power_sensor)
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return None
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return None
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        if unit and unit != UnitOfPower.WATT:
            try:
                value = PowerConverter.convert(value, unit, UnitOfPower.WATT)
            except (HomeAssistantError, KeyError, ValueError):
                _LOGGER.debug(
                    "Unknown power unit %r on %s, assuming W", unit, self.power_sensor
                )
        return value

    def get_available_power(self) -> float | None:
        """Return the power that can still be granted (None if unknown).

        Freshly-granted heaters are reserved for ``PENDING_POWER_SECONDS``
        because the power sensor needs some time to reflect them.
        """
        current = self.get_current_power()
        if current is None:
            return None
        return self.max_power - current - self._pending_power(time.monotonic())

    def room_status(self, room_id: str) -> str | None:
        """Return heating/waiting/idle/disabled for a room."""
        cfg = self.room_configs.get(room_id)
        state = self.room_states.get(room_id)
        if cfg is None or state is None:
            return None
        if not cfg.enabled:
            return STATUS_DISABLED
        if state.permission:
            return STATUS_HEATING
        if state.demand:
            return STATUS_WAITING
        return STATUS_IDLE

    async def async_set_target_temp(self, room_id: str, temp: float) -> None:
        """Change a room setpoint (persisted)."""
        if cfg := self.room_configs.get(room_id):
            cfg.target_temp = float(temp)
            self._async_persist()
            await self.async_refresh()

    async def async_set_room_enabled(self, room_id: str, enabled: bool) -> None:
        """Enable or disable a room (persisted)."""
        if cfg := self.room_configs.get(room_id):
            cfg.enabled = enabled
            self._async_persist()
            await self.async_refresh()

    async def async_set_max_power(self, watts: float) -> None:
        """Change the contracted power limit (persisted)."""
        self.max_power = float(watts)
        self._async_persist()
        await self.async_refresh()
        self._async_check_overpower()

    def is_runtime_only_update(self, options: Any) -> bool:
        """Return True if ``options`` is exactly what this coordinator persisted."""
        return self._expected_options is not None and dict(options) == self._expected_options

    # ── Persistence ───────────────────────────────────────────────────────

    @callback
    def _async_persist(self) -> None:
        """Write runtime values back into the config entry options."""
        options = copy.deepcopy(dict(self.config_entry.options))
        options[CONF_MAX_POWER] = self.max_power
        for room in options.get(CONF_ROOMS, []):
            if cfg := self.room_configs.get(room.get(CONF_ROOM_ID)):
                room[CONF_TARGET_TEMP] = cfg.target_temp
                room[CONF_ENABLED] = cfg.enabled
        self._expected_options = options
        self.hass.config_entries.async_update_entry(self.config_entry, options=options)

    # ── State listener ────────────────────────────────────────────────────

    @callback
    def _async_on_state_change(self, event: Event[EventStateChangedData]) -> None:
        if event.data["new_state"] is None:
            return

        if event.data["entity_id"] == self.power_sensor:
            self._async_check_overpower()
            if self._async_power_freed_for_waiting_room():
                self._async_schedule_refresh()
            # Keep the power sensors live without resetting the refresh timer.
            self.data = self._build_snapshot()
            self.async_update_listeners()
            return

        # A temperature sensor changed.
        self._async_schedule_refresh()

    @callback
    def _async_schedule_refresh(self) -> None:
        self.config_entry.async_create_background_task(
            self.hass, self.async_request_refresh(), f"{DOMAIN}_request_refresh"
        )

    @callback
    def _async_power_freed_for_waiting_room(self) -> bool:
        waiting = [
            self.room_configs[room_id].power_w
            for room_id, state in self.room_states.items()
            if state.demand and not state.permission and self.room_configs[room_id].enabled
        ]
        if not waiting or self._in_power_ok_grace(time.monotonic()):
            return False
        available = self.get_available_power()
        return available is not None and available >= min(waiting)

    # ── Overpower handling ────────────────────────────────────────────────

    @callback
    def _async_check_overpower(self) -> None:
        """Start (or cancel) the overpower grace timer based on the measurement."""
        current = self.get_current_power()
        task_running = self._overpower_task is not None and not self._overpower_task.done()

        if current is not None and current > self.max_power:
            if not task_running:
                self._overpower_task = self.config_entry.async_create_background_task(
                    self.hass, self._async_handle_overpower(), f"{DOMAIN}_overpower"
                )
        elif task_running and not self._shedding and current is not None:
            # Back under the limit during the grace period: nothing to do.
            self._overpower_task.cancel()
            self._overpower_task = None

    async def _async_handle_overpower(self) -> None:
        await asyncio.sleep(OVERPOWER_GRACE_SECONDS)
        self._shedding = True
        try:
            async with self._lock:
                await self._async_shed_load()
        finally:
            self._shedding = False
        self._async_publish()

    async def _async_shed_load(self) -> None:
        """Revoke permissions until the measured excess is covered."""
        current = self.get_current_power()
        if current is None:
            return
        excess = current - self.max_power
        if excess <= 0:
            return

        victims = self._shed_order()
        if not victims:
            _LOGGER.warning(
                "Over power budget by %.0fW but no heater is running — "
                "the excess comes from other loads",
                excess,
            )
            return

        _LOGGER.warning("Over power budget by %.0fW — shedding load", excess)
        freed = 0
        for room_id in victims:
            await self._async_set_permission(room_id, False, "overpower")
            freed += self.room_configs[room_id].power_w
            if freed >= excess:
                break
        self._last_shed_at = time.monotonic()

    def _shed_order(self) -> list[str]:
        """Rooms with permission, least important first.

        Highest priority number first, then smallest thermal deficit.
        """
        candidates = [
            room_id for room_id, state in self.room_states.items() if state.permission
        ]
        candidates.sort(
            key=lambda r: (-self.room_configs[r].priority, self.room_states[r].deficit)
        )
        return candidates

    # ── Control cycle ─────────────────────────────────────────────────────

    async def _async_control_cycle(self) -> None:
        now = time.monotonic()

        for room_id in list(self.room_configs):
            self._update_demand(room_id, now)

        # Revoke rooms that no longer need (or are no longer allowed) heat.
        for room_id, state in list(self.room_states.items()):
            cfg = self.room_configs[room_id]
            if state.permission and (not cfg.enabled or not state.demand):
                reason = "disabled" if not cfg.enabled else "target reached"
                await self._async_set_permission(room_id, False, reason)

        self._async_check_overpower()
        await self._async_grant_permissions(now)
        await self._async_reconcile(now)

    def _update_demand(self, room_id: str, now: float) -> None:
        """Software thermostat with hysteresis and sensor-failure fail-safe."""
        cfg = self.room_configs[room_id]
        state = self.room_states[room_id]

        temp = self._read_temperature(cfg.temp_sensor)
        state.current_temp = temp

        if not cfg.enabled:
            state.demand = False
            state.temp_unavailable_since = None
            return

        if temp is None:
            if state.temp_unavailable_since is None:
                state.temp_unavailable_since = now
            elif state.demand and now - state.temp_unavailable_since >= SENSOR_UNAVAILABLE_TIMEOUT:
                _LOGGER.warning(
                    "Room %s: temperature sensor %s unavailable for more than %ss — "
                    "stopping heating for safety",
                    room_id, cfg.temp_sensor, SENSOR_UNAVAILABLE_TIMEOUT,
                )
                state.demand = False
            return

        state.temp_unavailable_since = None
        state.deficit = cfg.target_temp - temp
        old_demand = state.demand
        if temp <= cfg.target_temp - self.cold_tolerance:
            state.demand = True
        elif temp >= cfg.target_temp + self.hot_tolerance:
            state.demand = False

        if state.demand != old_demand:
            _LOGGER.debug(
                "Room %s demand %s → %s (temp=%.2f, target=%.2f)",
                room_id, old_demand, state.demand, temp, cfg.target_temp,
            )

    def _in_power_ok_grace(self, now: float) -> bool:
        return self._last_shed_at is not None and now - self._last_shed_at < POWER_OK_GRACE_SECONDS

    def _pending_power(self, now: float) -> float:
        return sum(
            self.room_configs[room_id].power_w
            for room_id, state in self.room_states.items()
            if state.permission
            and state.granted_at is not None
            and now - state.granted_at < PENDING_POWER_SECONDS
        )

    async def _async_grant_permissions(self, now: float) -> None:
        """Grant permissions by priority, preempting less important rooms if needed."""
        current = self.get_current_power()
        if current is None:
            if not self._power_unavailable_logged:
                _LOGGER.warning(
                    "Power sensor %s is unavailable — no new heating permissions "
                    "will be granted until it reports again",
                    self.power_sensor,
                )
                self._power_unavailable_logged = True
            return
        self._power_unavailable_logged = False

        if current > self.max_power or self._shedding or self._in_power_ok_grace(now):
            return

        available = self.max_power - current - self._pending_power(now)
        waiting = [
            room_id
            for room_id, state in self.room_states.items()
            if state.demand and not state.permission and self.room_configs[room_id].enabled
        ]
        waiting.sort(
            key=lambda r: (self.room_configs[r].priority, -self.room_states[r].deficit)
        )

        for room_id in waiting:
            needed = self.room_configs[room_id].power_w
            if available >= needed:
                await self._async_set_permission(room_id, True, "power available")
                available -= needed
                continue

            victims = self._select_preemption_victims(room_id, needed - available, now)
            if not victims:
                continue
            for victim_id in victims:
                await self._async_set_permission(victim_id, False, f"preempted by {room_id}")
                available += self.room_configs[victim_id].power_w
            await self._async_set_permission(room_id, True, "preemption")
            available -= needed

    def _select_preemption_victims(
        self, room_id: str, shortfall: float, now: float
    ) -> list[str] | None:
        """Return the rooms to switch off so ``room_id`` can heat, or None.

        A heating room can be preempted when it is less important (higher
        priority number), or has the same priority but is at least
        ``PREEMPT_DEFICIT_MARGIN`` °C warmer relative to its setpoint. Rooms that
        started heating less than ``MIN_ON_TIME_SECONDS`` ago are protected.
        """
        cfg = self.room_configs[room_id]
        state = self.room_states[room_id]
        candidates: list[str] = []
        for victim_id, victim_state in self.room_states.items():
            if victim_id == room_id or not victim_state.permission:
                continue
            if (
                victim_state.granted_at is not None
                and now - victim_state.granted_at < MIN_ON_TIME_SECONDS
            ):
                continue
            victim_cfg = self.room_configs[victim_id]
            if victim_cfg.priority > cfg.priority or (
                victim_cfg.priority == cfg.priority
                and state.deficit - victim_state.deficit >= PREEMPT_DEFICIT_MARGIN
            ):
                candidates.append(victim_id)

        candidates.sort(
            key=lambda r: (-self.room_configs[r].priority, self.room_states[r].deficit)
        )
        chosen: list[str] = []
        freed = 0
        for victim_id in candidates:
            chosen.append(victim_id)
            freed += self.room_configs[victim_id].power_w
            if freed >= shortfall:
                return chosen
        return None

    async def _async_reconcile(self, now: float) -> None:
        """Make the real heaters match the desired state.

        Covers restarts/reloads (heaters left ON with no permission), failed
        commands and manual changes made outside of the controller.
        """
        for room_id, cfg in list(self.room_configs.items()):
            state = self.room_states[room_id]
            actual = self._heater_is_on(cfg)
            if actual is None or actual == state.permission:
                continue
            if (
                state.last_command_at is not None
                and now - state.last_command_at < RECONCILE_COOLDOWN_SECONDS
            ):
                continue
            _LOGGER.info(
                "Room %s: heater %s is %s but should be %s — correcting",
                room_id, cfg.heater_entity,
                "on" if actual else "off", "on" if state.permission else "off",
            )
            await self._async_apply_heater(room_id, state.permission)

    # ── Actuators ─────────────────────────────────────────────────────────

    async def _async_set_permission(self, room_id: str, active: bool, reason: str) -> None:
        state = self.room_states[room_id]
        state.permission = active
        state.granted_at = time.monotonic() if active else None
        _LOGGER.info(
            "Room %s: permission %s (%s)", room_id, "granted" if active else "revoked", reason
        )
        await self._async_apply_heater(room_id, active)

    async def _async_apply_heater(self, room_id: str, active: bool) -> None:
        cfg = self.room_configs.get(room_id)
        if cfg is None:
            return
        self.room_states[room_id].last_command_at = time.monotonic()
        try:
            if cfg.heater_type == HEATER_TYPE_CLIMATE:
                await self._async_apply_climate(cfg, active)
            else:
                await self.hass.services.async_call(
                    "switch",
                    "turn_on" if active else "turn_off",
                    {ATTR_ENTITY_ID: cfg.heater_entity},
                    blocking=False,
                )
        except (HomeAssistantError, vol.Invalid) as err:
            _LOGGER.error(
                "Room %s: could not turn %s heater %s: %s",
                room_id, "on" if active else "off", cfg.heater_entity, err,
            )

    async def _async_apply_climate(self, cfg: RoomConfig, active: bool) -> None:
        if not active:
            await self.hass.services.async_call(
                "climate",
                "set_hvac_mode",
                {ATTR_ENTITY_ID: cfg.heater_entity, ATTR_HVAC_MODE: HVACMode.OFF},
                blocking=False,
            )
            return

        data: dict[str, Any] = {
            ATTR_ENTITY_ID: cfg.heater_entity,
            ATTR_HVAC_MODE: cfg.hvac_mode_heat,
        }
        heater = self.hass.states.get(cfg.heater_entity)
        supports_temp = heater is not None and bool(
            int(heater.attributes.get(ATTR_SUPPORTED_FEATURES, 0))
            & ClimateEntityFeature.TARGET_TEMPERATURE
        )
        if cfg.force_max_temp and supports_temp:
            # One call: switch on in heat mode AND open the device fully, so the
            # software thermostat is the one deciding when to stop.
            data[ATTR_TEMPERATURE] = float(
                heater.attributes.get(ATTR_MAX_TEMP, FALLBACK_HEATER_MAX_TEMP)
            )
            await self.hass.services.async_call(
                "climate", "set_temperature", data, blocking=False
            )
        else:
            await self.hass.services.async_call(
                "climate", "set_hvac_mode", data, blocking=False
            )

    def _heater_is_on(self, cfg: RoomConfig) -> bool | None:
        state = self.hass.states.get(cfg.heater_entity)
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return None
        if cfg.heater_type == HEATER_TYPE_CLIMATE:
            return state.state != HVACMode.OFF
        return state.state == STATE_ON

    # ── Helpers ───────────────────────────────────────────────────────────

    def _read_temperature(self, entity_id: str) -> float | None:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    @callback
    def _async_publish(self) -> None:
        """Push a new snapshot to entities without resetting the refresh timer."""
        self.data = self._build_snapshot()
        self.async_update_listeners()

    def _build_snapshot(self) -> dict[str, Any]:
        return {
            "available_power": self.get_available_power(),
            "max_power": self.max_power,
            "current_power": self.get_current_power(),
            "rooms": {
                room_id: {
                    "name": cfg.room_name,
                    "enabled": cfg.enabled,
                    "status": self.room_status(room_id),
                    "demand": self.room_states[room_id].demand,
                    "permission": self.room_states[room_id].permission,
                    "deficit": self.room_states[room_id].deficit,
                    "current_temp": self.room_states[room_id].current_temp,
                    "target_temp": cfg.target_temp,
                    "power_w": cfg.power_w,
                    "priority": cfg.priority,
                }
                for room_id, cfg in self.room_configs.items()
            },
        }
