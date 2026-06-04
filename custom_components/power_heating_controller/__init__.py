"""Power Heating Controller — Home Assistant Integration.

FIX NOTES (this revision):
  - async_remove_room now calls the coordinator which returns the unique_id
    fragments; we then remove those entries from the entity registry so
    deleted rooms don't leave orphaned "integration no longer provides this
    entity" warnings.
  - handle_add_room now persists the new room into the config entry options so
    it survives restarts (previously add_room was runtime-only).
  - async_unload_entry properly cancels any pending coordinator tasks.
  - Services are re-registered on reload (guarded by has_service check).
"""

from __future__ import annotations

import logging
import re

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import entity_registry as er
import homeassistant.helpers.config_validation as cv

from .const import (
    CONF_HEATER_ENTITY,
    CONF_HEATER_TYPE,
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
    DEFAULT_HVAC_MODE_HEAT,
    DEFAULT_MAX_POWER,
    DEFAULT_POWER_W,
    DEFAULT_PRIORITY,
    DEFAULT_TARGET_TEMP,
    DOMAIN,
    HEATER_TYPE_CLIMATE,
    PLATFORMS,
    SERVICE_ADD_ROOM,
    SERVICE_DISABLE_ROOM,
    SERVICE_ENABLE_ROOM,
    SERVICE_REMOVE_ROOM,
    SERVICE_SET_MAX_POWER,
    SERVICE_SET_ROOM_TARGET,
)
from .coordinator import PowerHeatingCoordinator, RoomConfig

_LOGGER = logging.getLogger(__name__)

SERVICE_SET_MAX_POWER_SCHEMA = vol.Schema(
    {vol.Required("watts"): vol.All(vol.Coerce(float), vol.Range(min=500, max=20000))}
)
SERVICE_ADD_ROOM_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ROOM_NAME): cv.string,
        vol.Required(CONF_HEATER_TYPE): vol.In([HEATER_TYPE_CLIMATE, "switch"]),
        vol.Required(CONF_HEATER_ENTITY): cv.entity_id,
        vol.Required(CONF_TEMP_SENSOR): cv.entity_id,
        vol.Optional(CONF_TARGET_TEMP, default=DEFAULT_TARGET_TEMP): vol.Coerce(float),
        vol.Optional(CONF_POWER_W, default=DEFAULT_POWER_W): vol.All(vol.Coerce(int), vol.Range(min=100)),
        vol.Optional(CONF_PRIORITY, default=DEFAULT_PRIORITY): vol.All(vol.Coerce(int), vol.Range(min=1, max=10)),
    }
)
SERVICE_REMOVE_ROOM_SCHEMA = vol.Schema({vol.Required(CONF_ROOM_ID): cv.string})
SERVICE_SET_ROOM_TARGET_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ROOM_ID): cv.string,
        vol.Required(CONF_TARGET_TEMP): vol.All(vol.Coerce(float), vol.Range(min=5, max=35)),
    }
)
SERVICE_TOGGLE_ROOM_SCHEMA = vol.Schema({vol.Required(CONF_ROOM_ID): cv.string})


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = entry.options or entry.data

    rooms = [
        RoomConfig(
            room_id=r[CONF_ROOM_ID],
            room_name=r[CONF_ROOM_NAME],
            heater_type=r[CONF_HEATER_TYPE],
            heater_entity=r[CONF_HEATER_ENTITY],
            temp_sensor=r[CONF_TEMP_SENSOR],
            target_temp=r.get(CONF_TARGET_TEMP, DEFAULT_TARGET_TEMP),
            power_w=r.get(CONF_POWER_W, DEFAULT_POWER_W),
            priority=r.get(CONF_PRIORITY, DEFAULT_PRIORITY),
            hvac_mode_heat=r.get(CONF_HVAC_MODE_HEAT, DEFAULT_HVAC_MODE_HEAT),
            enabled=r.get("enabled", True),
        )
        for r in data.get(CONF_ROOMS, [])
    ]

    coordinator = PowerHeatingCoordinator(
        hass=hass,
        power_sensor=data[CONF_POWER_SENSOR],
        max_power=data.get(CONF_MAX_POWER, DEFAULT_MAX_POWER),
        rooms=rooms,
    )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await coordinator.async_setup()
    await coordinator.async_config_entry_first_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    _register_services(hass, entry, coordinator)

    _LOGGER.info(
        "Power Heating Controller loaded with %d rooms, max power %dW",
        len(rooms), coordinator.max_power,
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator: PowerHeatingCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
    await coordinator.async_unload()
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if not hass.data[DOMAIN]:
        for service in (
            SERVICE_SET_MAX_POWER, SERVICE_ADD_ROOM, SERVICE_REMOVE_ROOM,
            SERVICE_SET_ROOM_TARGET, SERVICE_ENABLE_ROOM, SERVICE_DISABLE_ROOM,
        ):
            hass.services.async_remove(DOMAIN, service)

    return unload_ok


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


def _register_services(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: PowerHeatingCoordinator,
) -> None:
    """Register all integration services. Guards against double-registration."""

    async def handle_set_max_power(call: ServiceCall) -> None:
        await coordinator.async_set_max_power(call.data["watts"])

    async def handle_add_room(call: ServiceCall) -> None:
        name = call.data[CONF_ROOM_NAME]
        room_id = re.sub(r"[^a-z0-9_]", "_", name.lower()).strip("_")
        cfg = RoomConfig(
            room_id=room_id,
            room_name=name,
            heater_type=call.data[CONF_HEATER_TYPE],
            heater_entity=call.data[CONF_HEATER_ENTITY],
            temp_sensor=call.data[CONF_TEMP_SENSOR],
            target_temp=call.data.get(CONF_TARGET_TEMP, DEFAULT_TARGET_TEMP),
            power_w=call.data.get(CONF_POWER_W, DEFAULT_POWER_W),
            priority=call.data.get(CONF_PRIORITY, DEFAULT_PRIORITY),
        )
        await coordinator.async_add_room(cfg)

        # Persist the new room into config entry options so it survives restarts
        current_options = dict(entry.options or entry.data)
        rooms = list(current_options.get(CONF_ROOMS, []))
        rooms.append({
            CONF_ROOM_ID: cfg.room_id,
            CONF_ROOM_NAME: cfg.room_name,
            CONF_HEATER_TYPE: cfg.heater_type,
            CONF_HEATER_ENTITY: cfg.heater_entity,
            CONF_TEMP_SENSOR: cfg.temp_sensor,
            CONF_TARGET_TEMP: cfg.target_temp,
            CONF_POWER_W: cfg.power_w,
            CONF_PRIORITY: cfg.priority,
            CONF_HVAC_MODE_HEAT: cfg.hvac_mode_heat,
            "enabled": cfg.enabled,
        })
        current_options[CONF_ROOMS] = rooms
        hass.config_entries.async_update_entry(entry, options=current_options)

    async def handle_remove_room(call: ServiceCall) -> None:
        room_id = call.data[CONF_ROOM_ID]

        # Ask coordinator to remove the room and get back the unique_id suffixes
        removed_suffixes = await coordinator.async_remove_room(room_id)

        # Clean orphaned entities from the entity registry
        ent_reg = er.async_get(hass)
        for suffix in removed_suffixes:
            unique_id = f"{entry.entry_id}_{suffix}"
            entity_entry = ent_reg.async_get_entity_id(
                # Try both climate and sensor platforms
                "climate" if suffix.endswith("_climate") else "sensor",
                DOMAIN,
                unique_id,
            )
            if entity_entry:
                ent_reg.async_remove(entity_entry)
                _LOGGER.info("Removed orphaned entity %s", entity_entry)

        # Persist removal into config entry options
        current_options = dict(entry.options or entry.data)
        current_options[CONF_ROOMS] = [
            r for r in current_options.get(CONF_ROOMS, [])
            if r.get(CONF_ROOM_ID) != room_id
        ]
        hass.config_entries.async_update_entry(entry, options=current_options)

    async def handle_set_room_target(call: ServiceCall) -> None:
        await coordinator.async_set_target_temp(call.data[CONF_ROOM_ID], call.data[CONF_TARGET_TEMP])

    async def handle_enable_room(call: ServiceCall) -> None:
        await coordinator.async_set_room_enabled(call.data[CONF_ROOM_ID], True)

    async def handle_disable_room(call: ServiceCall) -> None:
        await coordinator.async_set_room_enabled(call.data[CONF_ROOM_ID], False)

    if not hass.services.has_service(DOMAIN, SERVICE_SET_MAX_POWER):
        hass.services.async_register(DOMAIN, SERVICE_SET_MAX_POWER, handle_set_max_power, SERVICE_SET_MAX_POWER_SCHEMA)
        hass.services.async_register(DOMAIN, SERVICE_ADD_ROOM, handle_add_room, SERVICE_ADD_ROOM_SCHEMA)
        hass.services.async_register(DOMAIN, SERVICE_REMOVE_ROOM, handle_remove_room, SERVICE_REMOVE_ROOM_SCHEMA)
        hass.services.async_register(DOMAIN, SERVICE_SET_ROOM_TARGET, handle_set_room_target, SERVICE_SET_ROOM_TARGET_SCHEMA)
        hass.services.async_register(DOMAIN, SERVICE_ENABLE_ROOM, handle_enable_room, SERVICE_TOGGLE_ROOM_SCHEMA)
        hass.services.async_register(DOMAIN, SERVICE_DISABLE_ROOM, handle_disable_room, SERVICE_TOGGLE_ROOM_SCHEMA)
