"""Coordinator for Power Heating Controller.

This is the brain of the integration. It:
  1. Reads the household power consumption from a sensor (W, kW… are converted).
  2. Runs a software thermostat per room (demand ON/OFF with hysteresis).
  3. Grants or revokes heating permission per radiator so the total consumption
     stays within the contracted power limit.
  4. Gives primary radiators priority over support radiators, and verifies
     shutdown and fresh meter data before handing power to a waiting room.
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
from datetime import datetime, timedelta
import logging
from math import isfinite
import time
from typing import Any

import voluptuous as vol

from homeassistant.components.climate import (
    ATTR_HVAC_MODE,
    ATTR_HVAC_ACTION,
    ATTR_MAX_TEMP,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util.unit_conversion import PowerConverter, TemperatureConverter

from .const import (
    CONF_COLD_TOLERANCE,
    CONF_ENABLED,
    CONF_HEATER_ENTITY,
    CONF_HOT_TOLERANCE,
    CONF_MAX_POWER,
    CONF_POWER_SENSOR,
    CONF_POWER_W,
    CONF_PRIORITY,
    CONF_RADIATORS,
    CONF_ROOM_ID,
    CONF_ROOM_IDS,
    CONF_ROOM_NAME,
    CONF_ROOMS,
    CONF_TARGET_TEMP,
    CONF_TEMP_SENSOR,
    COORDINATOR_UPDATE_INTERVAL,
    DEFAULT_COLD_TOLERANCE,
    DEFAULT_HOT_TOLERANCE,
    DEFAULT_MAX_POWER,
    DEFAULT_POWER_W,
    DEFAULT_PRIORITY,
    DEFAULT_TARGET_TEMP,
    DOMAIN,
    FALLBACK_HEATER_MAX_TEMP,
    HEATER_SHUTDOWN_TIMEOUT,
    HEATER_TYPE_CLIMATE,
    MAX_PRIORITY,
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
    WAIT_ALLOCATION,
    WAIT_COOLDOWN,
    WAIT_NONE,
    WAIT_OVERPOWER,
    WAIT_POWER,
    WAIT_POWER_SENSOR,
    WAIT_RADIATOR,
    WAIT_SHUTDOWN,
    WAIT_TEMPERATURE_SENSOR,
)
from .radiator import Radiator, RadiatorState, collect_radiators

_LOGGER = logging.getLogger(__name__)

type PowerHeatingConfigEntry = ConfigEntry[PowerHeatingCoordinator]


@dataclass
class RoomConfig:
    """Configuration for one room."""

    room_id: str
    room_name: str
    heater_entity: str
    temp_sensor: str
    target_temp: float = DEFAULT_TARGET_TEMP
    power_w: int = DEFAULT_POWER_W
    priority: int = DEFAULT_PRIORITY
    enabled: bool = True

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RoomConfig:
        """Build a RoomConfig from a config entry room dict."""
        return cls(
            room_id=data[CONF_ROOM_ID],
            room_name=data[CONF_ROOM_NAME],
            heater_entity=data[CONF_HEATER_ENTITY],
            temp_sensor=data[CONF_TEMP_SENSOR],
            target_temp=float(data.get(CONF_TARGET_TEMP, DEFAULT_TARGET_TEMP)),
            power_w=int(data.get(CONF_POWER_W, DEFAULT_POWER_W)),
            priority=int(data.get(CONF_PRIORITY, DEFAULT_PRIORITY)),
            enabled=bool(data.get(CONF_ENABLED, True)),
        )


@dataclass
class RoomState:
    """Dynamic runtime state for one room (internal, not persisted)."""

    demand: bool = False
    deficit: float = 0.0
    current_temp: float | None = None
    temp_unavailable_since: float | None = None


@dataclass(frozen=True)
class PowerTransfer:
    """A handover that requires both a stopped radiator and a new meter reading."""

    previous: str
    requested: str
    last_power_report: datetime | None


class PowerHeatingCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Manages the power-aware heating logic for all rooms."""

    config_entry: PowerHeatingConfigEntry
    hub_device_id: str

    def __init__(self, hass: HomeAssistant, entry: PowerHeatingConfigEntry) -> None:
        """Initialise the coordinator from the config entry options."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=COORDINATOR_UPDATE_INTERVAL),
            always_update=False,
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
        self.radiators = collect_radiators(options)
        self.radiator_states = {entity_id: RadiatorState() for entity_id in self.radiators}
        self._index_radiators()
        self._transfer: PowerTransfer | None = None
        self._unloading = False

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
        entities.update(self.radiators)
        self._unsub_listeners.append(
            async_track_state_change_event(
                self.hass, sorted(entities), self._async_on_state_change
            )
        )

    async def async_unload(self) -> None:
        """Remove listeners and cancel pending tasks."""
        self._unloading = True
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
            except (HomeAssistantError, KeyError, TypeError, ValueError):
                return None
        return value if isfinite(value) else None

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
        if self.room_has_permission(room_id):
            return STATUS_HEATING
        if state.demand:
            return STATUS_WAITING
        return STATUS_IDLE

    def room_has_permission(self, room_id: str) -> bool:
        room = self.room_configs.get(room_id)
        if room is None or not room.enabled or not self.room_states[room_id].demand:
            return False
        return any(
            self.radiator_states[entity_id].permission
            for entity_id in self.room_radiators.get(room_id, ())
        )

    def _index_radiators(self) -> None:
        self.room_radiators: dict[str, list[str]] = {}
        for entity_id, radiator in self.radiators.items():
            for room_id in radiator.room_ids:
                self.room_radiators.setdefault(room_id, []).append(entity_id)

    def _room_waiting_reason(
        self, room_id: str, available: float | None, power_block: str | None
    ) -> str:
        """Describe what is preventing this room from heating."""
        cfg = self.room_configs[room_id]
        state = self.room_states[room_id]
        if not cfg.enabled:
            return WAIT_NONE
        if state.current_temp is None:
            return WAIT_TEMPERATURE_SENSOR
        if not state.demand:
            return WAIT_NONE
        if self._transfer is not None and self._transfer.requested == cfg.heater_entity:
            return WAIT_SHUTDOWN
        primary = self.radiator_states.get(cfg.heater_entity)
        if primary is not None and primary.permission:
            return WAIT_NONE

        if power_block is not None:
            return power_block
        radiator = self.radiators.get(cfg.heater_entity)
        if radiator is not None and self._heater_is_on(radiator) is None:
            return WAIT_RADIATOR
        if available is not None and available < cfg.power_w:
            return WAIT_POWER
        return WAIT_ALLOCATION

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

    async def async_remove_room(self, room_id: str) -> None:
        """Remove a room and retire the radiators that would lose their last link."""
        await self._async_edit_room(room_id, None)

    async def async_replace_room_heater(
        self, room_id: str, old_heater: str, changes: dict[str, Any]
    ) -> None:
        """Save a replacement only after the previous radiator is safe to release."""
        await self._async_edit_room(room_id, changes, old_heater)

    async def _async_edit_room(
        self, room_id: str, changes: dict[str, Any] | None, old_heater: str | None = None
    ) -> None:
        async with self._lock:
            options = self.config_entry.options
            rooms = options.get(CONF_ROOMS, [])
            room = next((room for room in rooms if room[CONF_ROOM_ID] == room_id), None)
            if room is None or room_id not in self.room_configs:
                raise self._room_error("room_not_found", room_id)
            if old_heater is not None and room[CONF_HEATER_ENTITY] != old_heater:
                raise self._room_error("room_changed", room_id)
            if changes and changes[CONF_HEATER_ENTITY] in self.radiators:
                raise self._room_error("heater_already_configured", room_id)

            new_rooms = [record for record in rooms if record[CONF_ROOM_ID] != room_id]
            if changes is not None:
                new_rooms = [
                    {**room, **changes} if record is room else record for record in rooms
                ]

            new_options = {**options, CONF_ROOMS: new_rooms}
            if changes is None:
                new_options[CONF_RADIATORS] = [
                    {**radiator, CONF_ROOM_IDS: [link for link in radiator[CONF_ROOM_IDS] if link != room_id]}
                    for radiator in options.get(CONF_RADIATORS, [])
                    if any(link != room_id for link in radiator[CONF_ROOM_IDS])
                ]
            await self._async_save_radiator_changes(new_options)
            # Keep a replaced room and its entity IDs until the reload.
            if changes is None:
                del self.room_configs[room_id]
                del self.room_states[room_id]

    async def async_save_support(
        self, previous_entity: str | None, support: dict[str, Any] | None
    ) -> None:
        """Attach a support radiator, change its links or safely remove it."""
        async with self._lock:
            options = self.config_entry.options
            supports = list(options.get(CONF_RADIATORS, []))
            existing = next(
                (record for record in supports if record[CONF_HEATER_ENTITY] == previous_entity),
                None,
            )
            if previous_entity is not None and existing is None:
                raise self._room_error("room_changed")
            if support is not None:
                entity_id = support[CONF_HEATER_ENTITY]
                if entity_id != previous_entity and entity_id in self.radiators:
                    raise self._room_error("heater_already_configured")
                if not set(support[CONF_ROOM_IDS]).issubset(self.room_configs):
                    raise self._room_error("room_changed")
            if existing is None:
                if support is not None:
                    supports.append(support)
            elif support is None:
                supports.remove(existing)
            else:
                supports[supports.index(existing)] = support
            await self._async_save_radiator_changes({**options, CONF_RADIATORS: supports})

    async def _async_save_radiator_changes(self, options: dict[str, Any]) -> None:
        """Retire only devices absent from the new configuration."""
        previous_options = self.config_entry.options
        remaining = collect_radiators(options)
        retired = self.radiators.keys() - remaining.keys()
        for entity_id in sorted(retired):
            try:
                await self._async_confirm_heater_off(self.radiators[entity_id])
            except (HomeAssistantError, vol.Invalid, TimeoutError) as err:
                _LOGGER.warning("Could not confirm shutdown of %s: %s", entity_id, str(err) or type(err).__name__)
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="heater_shutdown_failed",
                    translation_placeholders={"entity_id": entity_id},
                ) from err
        if self.config_entry.options != previous_options or self._unloading:
            raise self._room_error("room_changed")
        # Refresh will rebuild subscriptions and add newly configured devices.
        self.hass.config_entries.async_update_entry(self.config_entry, options=options)
        self.radiators = {key: remaining[key] for key in self.radiators if key in remaining}
        for entity_id in retired:
            del self.radiator_states[entity_id]
        self._index_radiators()

    @staticmethod
    def _room_error(key: str, room_id: str = "") -> ServiceValidationError:
        return ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key=key,
            translation_placeholders={"room_id": room_id} if key == "room_not_found" else None,
        )

    async def _async_confirm_heater_off(self, cfg: Radiator) -> None:
        """Wait for the switch or climate entity to report that it is off."""
        state = self.hass.states.get(cfg.heater_entity)
        if (
            state is not None
            and state.state == STATE_OFF
            and not self.radiator_states[cfg.heater_entity].permission
        ):
            return

        state_changed = asyncio.Event()

        @callback
        def _async_heater_changed(_event: Event[EventStateChangedData]) -> None:
            state_changed.set()

        unsubscribe = async_track_state_change_event(
            self.hass, [cfg.heater_entity], _async_heater_changed
        )
        try:
            async with asyncio.timeout(HEATER_SHUTDOWN_TIMEOUT):
                await self._async_turn_heater_off(cfg, blocking=True)
                while (state := self.hass.states.get(cfg.heater_entity)) is None or state.state != STATE_OFF:
                    state_changed.clear()
                    await state_changed.wait()
        finally:
            unsubscribe()

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
        entity_id = event.data["entity_id"]
        if entity_id == self.power_sensor:
            self._async_check_overpower()
            if self._transfer is not None or self._async_power_freed_for_waiting_room():
                self._async_schedule_refresh()
            # Keep the power sensors live without resetting the refresh timer.
            self._async_publish()
            return

        if entity_id in self.radiators:
            if self._transfer is not None and entity_id == self._transfer.previous:
                self._async_schedule_refresh()
            self._async_publish()
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
        waiting = self._waiting_radiators()
        if not waiting or self._in_power_ok_grace(time.monotonic()):
            return False
        available = self.get_available_power()
        principals = [radiator for radiator in waiting if radiator.primary]
        return available is not None and available >= min(
            radiator.power_w for radiator in (principals or waiting)
        )

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
        """Switch off supports first and only credit a reported shutdown."""
        current = self.get_current_power()
        if current is None or current <= self.max_power:
            return
        excess = current - self.max_power
        victims = sorted(
            (
                entity_id for entity_id, radiator in self.radiators.items()
                if self.radiator_states[entity_id].permission or self._heater_is_on(radiator)
            ),
            key=self._shed_order,
        )
        if not victims:
            _LOGGER.warning("Over power budget by %.0f W with no managed radiator running", excess)
            return

        self._last_shed_at = time.monotonic()
        for entity_id in victims:
            radiator = self.radiators[entity_id]
            state = self.radiator_states[entity_id]
            try:
                await self._async_confirm_heater_off(radiator)
            except (HomeAssistantError, vol.Invalid, TimeoutError):
                _LOGGER.warning("Radiator %s did not confirm shutdown during load shedding", entity_id)
            else:
                excess -= radiator.power_w
                state.granted_at = None
            finally:
                state.permission = False
                state.last_command_at = time.monotonic()
            if excess <= 0:
                break

    # ── Control cycle ─────────────────────────────────────────────────────

    async def _async_control_cycle(self) -> None:
        now = time.monotonic()
        for room_id in self.room_configs:
            self._update_demand(room_id, now)

        for entity_id, radiator in self.radiators.items():
            if self.radiator_states[entity_id].permission and not self._demanders(radiator):
                await self._async_set_permission(entity_id, False, "no linked room requests heat")

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
        transferring = self._transfer.previous if self._transfer is not None else None
        return sum(
            self.radiators[entity_id].power_w
            for entity_id, state in self.radiator_states.items()
            if state.granted_at is not None
            and now - state.granted_at < PENDING_POWER_SECONDS
            and (
                state.permission
                or entity_id == transferring
                or self._heater_is_on(self.radiators[entity_id]) is not False
            )
        )

    def _demanders(self, radiator: Radiator, *, fresh: bool = False) -> list[str]:
        return [
            room_id for room_id in radiator.room_ids
            if (state := self.room_states.get(room_id)) is not None
            and self.room_configs[room_id].enabled
            and state.demand
            and (not fresh or state.current_temp is not None)
        ]

    def _priority(self, radiator: Radiator) -> tuple[int, float]:
        return min(
            ((self.room_configs[room_id].priority, -self.room_states[room_id].deficit)
             for room_id in self._demanders(radiator)),
            default=(MAX_PRIORITY + 1, 0.0),
        )

    def _shed_order(self, entity_id: str) -> tuple[bool, int, float, int]:
        radiator = self.radiators[entity_id]
        priority, deficit = self._priority(radiator)
        return radiator.primary, -priority, -deficit, -radiator.order

    def _waiting_radiators(self) -> list[Radiator]:
        return [
            radiator for entity_id, radiator in self.radiators.items()
            if not self.radiator_states[entity_id].permission and self._demanders(radiator, fresh=True)
        ]

    def _update_transfer(self, waiting: set[str]) -> None:
        transfer = self._transfer
        if transfer is None:
            return
        previous = self.hass.states.get(transfer.previous)
        power = self.hass.states.get(self.power_sensor)
        if (
            transfer.requested not in waiting
            or transfer.previous not in self.radiators
            or (
                previous is not None and previous.state == STATE_OFF
                and power is not None and power.last_reported >= previous.last_changed
                and (transfer.last_power_report is None or power.last_reported > transfer.last_power_report)
            )
        ):
            self._transfer = None

    async def _async_grant_permissions(self, now: float) -> None:
        current = self.get_current_power()
        if current is None:
            if not self._power_unavailable_logged:
                _LOGGER.warning("Power sensor %s is unavailable or invalid; heating starts are paused", self.power_sensor)
                self._power_unavailable_logged = True
            return
        self._power_unavailable_logged = False

        if current > self.max_power or self._shedding or self._in_power_ok_grace(now):
            return

        waiting = self._waiting_radiators()
        self._update_transfer({radiator.heater_entity for radiator in waiting})
        available = self.max_power - current - self._pending_power(now)
        waiting.sort(key=lambda radiator: (not radiator.primary, self._priority(radiator), radiator.order))
        unmet_primary = False
        for radiator in waiting:
            entity_id = radiator.heater_entity
            transfer = self._transfer
            if not radiator.primary and (unmet_primary or transfer is not None):
                break
            if (
                self._heater_is_on(radiator) is not None
                and available >= radiator.power_w
                and (transfer is None or entity_id not in (transfer.requested, transfer.previous))
            ):
                await self._async_set_permission(entity_id, True, "power available")
                available -= radiator.power_w
                continue
            if not radiator.primary:
                continue
            unmet_primary = True
            if transfer is None and self._heater_is_on(radiator) is not None:
                victim = self._preemption_victim(radiator, radiator.power_w - available, now)
                if victim is not None:
                    power_sample = self.hass.states.get(self.power_sensor)
                    self._transfer = PowerTransfer(
                        victim, entity_id, power_sample.last_reported if power_sample else None
                    )
                    await self._async_set_permission(victim, False, f"power requested by {entity_id}")

    def _preemption_victim(self, requested: Radiator, shortfall: float, now: float) -> str | None:
        """Choose the first radiator to stop if eligible loads can cover the shortfall."""
        if shortfall <= 0:
            return None
        priority, deficit = self._priority(requested)
        candidates = []
        for entity_id, radiator in self.radiators.items():
            state = self.radiator_states[entity_id]
            if not state.permission or self._heater_is_on(radiator) is not True:
                continue
            if radiator.primary:
                if set(requested.room_ids).intersection(radiator.room_ids):
                    continue
                if state.granted_at is not None and now - state.granted_at < MIN_ON_TIME_SECONDS:
                    continue
                other_priority, other_deficit = self._priority(radiator)
                if other_priority <= priority and (
                    other_priority < priority or other_deficit - deficit < PREEMPT_DEFICIT_MARGIN
                ):
                    continue
            candidates.append(entity_id)
        if sum(self.radiators[entity_id].power_w for entity_id in candidates) < shortfall:
            return None
        return min(candidates, key=self._shed_order)

    async def _async_reconcile(self, now: float) -> None:
        """Make the real heaters match the desired state.

        Covers restarts/reloads (heaters left ON with no permission), failed
        commands and manual changes made outside of the controller.
        """
        for entity_id, cfg in self.radiators.items():
            state = self.radiator_states[entity_id]
            actual = self._heater_is_on(cfg)
            if actual is None or actual == state.permission:
                continue
            if (
                state.last_command_at is not None
                and now - state.last_command_at < RECONCILE_COOLDOWN_SECONDS
            ):
                continue
            _LOGGER.info(
                "Radiator %s is %s but should be %s — correcting",
                entity_id,
                "on" if actual else "off", "on" if state.permission else "off",
            )
            await self._async_apply_heater(entity_id, state.permission)

    # ── Actuators ─────────────────────────────────────────────────────────

    async def _async_set_permission(self, entity_id: str, active: bool, reason: str) -> None:
        state = self.radiator_states[entity_id]
        state.permission = active
        if active:
            state.granted_at = time.monotonic()
        _LOGGER.info("Radiator %s: permission %s (%s)", entity_id, "granted" if active else "revoked", reason)
        await self._async_apply_heater(entity_id, active)

    async def _async_apply_heater(self, entity_id: str, active: bool) -> None:
        cfg = self.radiators[entity_id]
        self.radiator_states[entity_id].last_command_at = time.monotonic()
        try:
            if not active:
                await self._async_turn_heater_off(cfg)
            elif cfg.heater_type == HEATER_TYPE_CLIMATE:
                await self._async_turn_climate_on(cfg)
            else:
                await self.hass.services.async_call(
                    "switch",
                    "turn_on",
                    {ATTR_ENTITY_ID: cfg.heater_entity},
                    blocking=False,
                )
        except (HomeAssistantError, vol.Invalid) as err:
            _LOGGER.error(
                "Could not turn %s radiator %s: %s",
                "on" if active else "off", entity_id, err,
            )

    async def _async_turn_heater_off(self, cfg: Radiator, *, blocking: bool = False) -> None:
        if cfg.heater_type == HEATER_TYPE_CLIMATE:
            await self.hass.services.async_call(
                "climate",
                "set_hvac_mode",
                {ATTR_ENTITY_ID: cfg.heater_entity, ATTR_HVAC_MODE: HVACMode.OFF},
                blocking=blocking,
            )
        else:
            await self.hass.services.async_call(
                "switch",
                "turn_off",
                {ATTR_ENTITY_ID: cfg.heater_entity},
                blocking=blocking,
            )

    async def _async_turn_climate_on(self, cfg: Radiator) -> None:
        # 1. Turn on the heater in the configured HVAC mode (e.g. "heat")
        await self.hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {ATTR_ENTITY_ID: cfg.heater_entity, ATTR_HVAC_MODE: cfg.hvac_mode_heat},
            blocking=False,
        )

        # 2. Optionally push the hardware thermostat to its maximum temperature
        if cfg.force_max_temp:
            heater = self.hass.states.get(cfg.heater_entity)
            max_temp = float(
                heater.attributes.get(ATTR_MAX_TEMP, FALLBACK_HEATER_MAX_TEMP)
                if heater is not None
                else FALLBACK_HEATER_MAX_TEMP
            )
            try:
                await self.hass.services.async_call(
                    "climate",
                    "set_temperature",
                    {ATTR_ENTITY_ID: cfg.heater_entity, ATTR_TEMPERATURE: max_temp},
                    blocking=False,
                )
            except (HomeAssistantError, vol.Invalid) as err:
                _LOGGER.debug(
                    "Could not set max temperature on %s: %s",
                    cfg.heater_entity,
                    err,
                )


    def _heater_is_on(self, cfg: Radiator) -> bool | None:
        state = self.hass.states.get(cfg.heater_entity)
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return None
        if cfg.heater_type == HEATER_TYPE_CLIMATE:
            return state.state != HVACMode.OFF
        if state.state == STATE_ON:
            return True
        return False if state.state == STATE_OFF else None

    # ── Helpers ───────────────────────────────────────────────────────────

    def _read_temperature(self, entity_id: str) -> float | None:
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNKNOWN, STATE_UNAVAILABLE):
            return None
        try:
            value = float(state.state)
            unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT) or UnitOfTemperature.CELSIUS
            value = TemperatureConverter.convert(value, unit, UnitOfTemperature.CELSIUS)
        except (HomeAssistantError, KeyError, TypeError, ValueError):
            return None
        return value if isfinite(value) else None

    def _radiator_snapshot(self, entity_id: str, now: float) -> dict[str, Any]:
        radiator = self.radiators[entity_id]
        command = self.radiator_states[entity_id]
        reported = self.hass.states.get(entity_id)
        actual = self._heater_is_on(radiator)
        return {
            "entity_id": entity_id,
            "role": "primary" if radiator.primary else (
                "shared" if len(radiator.room_ids) > 1 else "support"
            ),
            "room_ids": list(radiator.room_ids),
            "power_w": radiator.power_w,
            "permission": command.permission,
            "reported_state": reported.state if reported else STATE_UNAVAILABLE,
            "hvac_action": reported.attributes.get(ATTR_HVAC_ACTION) if reported else None,
            "needs_attention": (
                actual is None or (
                    actual != command.permission
                    and (command.last_command_at is None or now - command.last_command_at >= HEATER_SHUTDOWN_TIMEOUT)
                )
            ),
        }

    @callback
    def _async_publish(self) -> None:
        """Push a new snapshot to entities without resetting the refresh timer."""
        snapshot = self._build_snapshot()
        if snapshot == self.data:
            return
        self.data = snapshot
        self.async_update_listeners()

    def _build_snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        current = self.get_current_power()
        available = None if current is None else self.max_power - current - self._pending_power(now)
        power_block = None
        if current is None:
            power_block = WAIT_POWER_SENSOR
        elif current > self.max_power or self._shedding:
            power_block = WAIT_OVERPOWER
        elif self._in_power_ok_grace(now):
            power_block = WAIT_COOLDOWN

        radiators = {
            entity_id: self._radiator_snapshot(entity_id, now) for entity_id in self.radiators
        }
        return {
            "available_power": available,
            "max_power": self.max_power,
            "current_power": current,
            "rooms": {
                room_id: {
                    "name": cfg.room_name,
                    "enabled": cfg.enabled,
                    "status": self.room_status(room_id),
                    "waiting_reason": self._room_waiting_reason(room_id, available, power_block),
                    "demand": self.room_states[room_id].demand,
                    "permission": self.room_has_permission(room_id),
                    "deficit": self.room_states[room_id].deficit,
                    "current_temp": self.room_states[room_id].current_temp,
                    "target_temp": cfg.target_temp,
                    "power_w": cfg.power_w,
                    "priority": cfg.priority,
                    "radiators": [
                        radiators[entity_id] for entity_id in self.room_radiators.get(room_id, ())
                    ],
                }
                for room_id, cfg in self.room_configs.items()
            },
        }
