"""Services for Power Heating Controller."""

from __future__ import annotations

import copy

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_ENABLED,
    CONF_FORCE_MAX_TEMP,
    CONF_HEATER_ENTITY,
    CONF_HEATER_TYPE,
    CONF_HVAC_MODE_HEAT,
    CONF_POWER_W,
    CONF_PRIORITY,
    CONF_ROOM_ID,
    CONF_ROOM_NAME,
    CONF_ROOMS,
    CONF_TARGET_TEMP,
    CONF_TEMP_SENSOR,
    DEFAULT_FORCE_MAX_TEMP,
    DEFAULT_HVAC_MODE_HEAT,
    DEFAULT_POWER_W,
    DEFAULT_PRIORITY,
    DEFAULT_TARGET_TEMP,
    DOMAIN,
    HEATER_TYPES,
    MAX_MAX_POWER,
    MAX_PRIORITY,
    MAX_ROOM_POWER,
    MAX_TARGET_TEMP,
    MIN_MAX_POWER,
    MIN_PRIORITY,
    MIN_ROOM_POWER,
    MIN_TARGET_TEMP,
    SERVICE_ADD_ROOM,
    SERVICE_DISABLE_ROOM,
    SERVICE_ENABLE_ROOM,
    SERVICE_REMOVE_ROOM,
    SERVICE_SET_MAX_POWER,
    SERVICE_SET_ROOM_TARGET,
)
from .coordinator import PowerHeatingConfigEntry, PowerHeatingCoordinator
from .helpers import make_room_id

_TARGET_TEMP = vol.All(vol.Coerce(float), vol.Range(min=MIN_TARGET_TEMP, max=MAX_TARGET_TEMP))

SET_MAX_POWER_SCHEMA = vol.Schema(
    {vol.Required("watts"): vol.All(vol.Coerce(float), vol.Range(min=MIN_MAX_POWER, max=MAX_MAX_POWER))}
)
ADD_ROOM_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_ROOM_NAME): cv.string,
        vol.Required(CONF_HEATER_TYPE): vol.In(HEATER_TYPES),
        vol.Required(CONF_HEATER_ENTITY): cv.entity_id,
        vol.Required(CONF_TEMP_SENSOR): cv.entity_id,
        vol.Optional(CONF_TARGET_TEMP, default=DEFAULT_TARGET_TEMP): _TARGET_TEMP,
        vol.Optional(CONF_POWER_W, default=DEFAULT_POWER_W): vol.All(
            vol.Coerce(int), vol.Range(min=MIN_ROOM_POWER, max=MAX_ROOM_POWER)
        ),
        vol.Optional(CONF_PRIORITY, default=DEFAULT_PRIORITY): vol.All(
            vol.Coerce(int), vol.Range(min=MIN_PRIORITY, max=MAX_PRIORITY)
        ),
        vol.Optional(CONF_HVAC_MODE_HEAT, default=DEFAULT_HVAC_MODE_HEAT): cv.string,
        vol.Optional(CONF_FORCE_MAX_TEMP, default=DEFAULT_FORCE_MAX_TEMP): cv.boolean,
    }
)
ROOM_SCHEMA = vol.Schema({vol.Required(CONF_ROOM_ID): cv.string})
SET_ROOM_TARGET_SCHEMA = vol.Schema(
    {vol.Required(CONF_ROOM_ID): cv.string, vol.Required(CONF_TARGET_TEMP): _TARGET_TEMP}
)


def _get_entry(hass: HomeAssistant) -> PowerHeatingConfigEntry:
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is ConfigEntryState.LOADED:
            return entry
    raise ServiceValidationError(translation_domain=DOMAIN, translation_key="not_loaded")


def _get_coordinator(hass: HomeAssistant) -> PowerHeatingCoordinator:
    return _get_entry(hass).runtime_data


def _check_room(coordinator: PowerHeatingCoordinator, room_id: str) -> None:
    if not coordinator.has_room(room_id):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="room_not_found",
            translation_placeholders={"room_id": room_id},
        )


@callback
def async_register_services(hass: HomeAssistant) -> None:
    """Register the integration services."""

    async def set_max_power(call: ServiceCall) -> None:
        await _get_coordinator(hass).async_set_max_power(call.data["watts"])

    async def set_room_target(call: ServiceCall) -> None:
        coordinator = _get_coordinator(hass)
        _check_room(coordinator, call.data[CONF_ROOM_ID])
        await coordinator.async_set_target_temp(call.data[CONF_ROOM_ID], call.data[CONF_TARGET_TEMP])

    async def enable_room(call: ServiceCall) -> None:
        coordinator = _get_coordinator(hass)
        _check_room(coordinator, call.data[CONF_ROOM_ID])
        await coordinator.async_set_room_enabled(call.data[CONF_ROOM_ID], True)

    async def disable_room(call: ServiceCall) -> None:
        coordinator = _get_coordinator(hass)
        _check_room(coordinator, call.data[CONF_ROOM_ID])
        await coordinator.async_set_room_enabled(call.data[CONF_ROOM_ID], False)

    async def add_room(call: ServiceCall) -> None:
        entry = _get_entry(hass)
        name = call.data[CONF_ROOM_NAME].strip()
        room_id = make_room_id(name)
        heater_type = call.data[CONF_HEATER_TYPE]
        heater_entity = call.data[CONF_HEATER_ENTITY]

        if not room_id:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="invalid_name")
        if entry.runtime_data.has_room(room_id):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="room_exists",
                translation_placeholders={"room_id": room_id},
            )
        if not heater_entity.startswith(f"{heater_type}."):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="wrong_heater_domain",
                translation_placeholders={"entity_id": heater_entity, "heater_type": heater_type},
            )

        options = copy.deepcopy(dict(entry.options))
        options.setdefault(CONF_ROOMS, []).append(
            {
                CONF_ROOM_ID: room_id,
                CONF_ROOM_NAME: name,
                CONF_HEATER_TYPE: heater_type,
                CONF_HEATER_ENTITY: heater_entity,
                CONF_TEMP_SENSOR: call.data[CONF_TEMP_SENSOR],
                CONF_TARGET_TEMP: call.data[CONF_TARGET_TEMP],
                CONF_POWER_W: call.data[CONF_POWER_W],
                CONF_PRIORITY: call.data[CONF_PRIORITY],
                CONF_HVAC_MODE_HEAT: call.data[CONF_HVAC_MODE_HEAT],
                CONF_FORCE_MAX_TEMP: call.data[CONF_FORCE_MAX_TEMP],
                CONF_ENABLED: True,
            }
        )
        # Structural change → the update listener reloads the entry.
        hass.config_entries.async_update_entry(entry, options=options)

    async def remove_room(call: ServiceCall) -> None:
        await _get_coordinator(hass).async_remove_room(call.data[CONF_ROOM_ID])

    hass.services.async_register(DOMAIN, SERVICE_SET_MAX_POWER, set_max_power, SET_MAX_POWER_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_SET_ROOM_TARGET, set_room_target, SET_ROOM_TARGET_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_ENABLE_ROOM, enable_room, ROOM_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_DISABLE_ROOM, disable_room, ROOM_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_ADD_ROOM, add_room, ADD_ROOM_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_REMOVE_ROOM, remove_room, ROOM_SCHEMA)

