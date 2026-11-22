"""Power Heating Controller — Home Assistant integration.

The config entry options are the single source of truth for the configuration
AND for the runtime values (target temperatures, enabled flags, max power).
Structural changes (rooms added/removed/edited, sensors changed) reload the
entry; runtime-only changes written by the coordinator do not.
"""

from __future__ import annotations

import copy
import logging

import voluptuous as vol

from homeassistant.components.climate import ATTR_HVAC_MODE, HVACMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.typing import ConfigType

from .const import (
    DOMAIN,
    HEATER_TYPE_CLIMATE,
    MANUFACTURER,
    PLATFORMS,
    VERSION,
)
from .coordinator import PowerHeatingConfigEntry, PowerHeatingCoordinator
from .entity import room_device_identifier
from .radiator import collect_radiators
from .services import async_register_services

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration services (once, independent of entries)."""
    async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: PowerHeatingConfigEntry) -> bool:
    """Set up Power Heating Controller from a config entry."""
    # Entries created by v1.x may only have `data`: copy it into options once.
    if not entry.options:
        hass.config_entries.async_update_entry(
            entry, options=copy.deepcopy(dict(entry.data))
        )

    coordinator = PowerHeatingCoordinator(hass, entry)
    entry.runtime_data = coordinator

    coordinator.hub_device_id = _async_setup_devices(hass, entry, coordinator)

    await coordinator.async_setup()
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        await coordinator.async_unload()
        raise

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    _LOGGER.info(
        "Power Heating Controller loaded with %d rooms, max power %.0fW",
        len(coordinator.room_configs), coordinator.max_power,
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PowerHeatingConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_unload()
    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Turn every managed heater off when the integration is deleted."""
    for heater, radiator in collect_radiators(entry.options or entry.data).items():
        try:
            if radiator.heater_type == HEATER_TYPE_CLIMATE:
                await hass.services.async_call(
                    "climate", "set_hvac_mode",
                    {ATTR_ENTITY_ID: heater, ATTR_HVAC_MODE: HVACMode.OFF},
                    blocking=False,
                )
            else:
                await hass.services.async_call(
                    "switch", "turn_off", {ATTR_ENTITY_ID: heater}, blocking=False
                )
        except (HomeAssistantError, vol.Invalid) as err:
            _LOGGER.warning("Could not turn off %s: %s", heater, err)


async def _async_update_listener(hass: HomeAssistant, entry: PowerHeatingConfigEntry) -> None:
    """Reload only on structural changes, not on runtime values we persisted."""
    if entry.runtime_data.is_runtime_only_update(entry.options):
        return
    await hass.config_entries.async_reload(entry.entry_id)


@callback
def _async_setup_devices(
    hass: HomeAssistant,
    entry: PowerHeatingConfigEntry,
    coordinator: PowerHeatingCoordinator,
) -> str:
    """Return the hub device ID after removing devices and entities of deleted rooms."""
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    entry_id = entry.entry_id

    hub = dev_reg.async_get_or_create(
        config_entry_id=entry_id,
        identifiers={(DOMAIN, entry_id)},
        name="Power Heating Controller",
        manufacturer=MANUFACTURER,
        model="Power Heating Controller",
        sw_version=VERSION,
    )

    valid_unique_ids = {
        f"{entry_id}_available_power",
        f"{entry_id}_current_power",
        f"{entry_id}_max_power",
    }
    valid_devices = {(DOMAIN, entry_id)}
    for room_id in coordinator.room_configs:
        valid_unique_ids.add(f"{entry_id}_{room_id}_climate")
        valid_unique_ids.add(f"{entry_id}_{room_id}_status")
        valid_unique_ids.add(f"{entry_id}_{room_id}_waiting_reason")
        valid_devices.add(room_device_identifier(entry_id, room_id))

    for entity in er.async_entries_for_config_entry(ent_reg, entry_id):
        if entity.unique_id not in valid_unique_ids:
            _LOGGER.info("Removing orphaned entity %s", entity.entity_id)
            ent_reg.async_remove(entity.entity_id)

    for device in dr.async_entries_for_config_entry(dev_reg, entry_id):
        if not device.identifiers & valid_devices:
            _LOGGER.info("Removing orphaned device %s", device.name)
            if hasattr(device, "config_entry_id"):
                dev_reg.async_remove_device(device.id)
            else:
                dev_reg.async_update_device(device.id, remove_config_entry_id=entry_id)

    return hub.id
