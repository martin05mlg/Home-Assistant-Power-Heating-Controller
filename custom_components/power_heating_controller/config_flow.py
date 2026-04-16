"""Config flow for Power Heating Controller."""

from __future__ import annotations

import logging
import re
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers.selector import (
    EntitySelector, EntitySelectorConfig,
    NumberSelector, NumberSelectorConfig, NumberSelectorMode,
    SelectSelector, SelectSelectorConfig, SelectSelectorMode,
    TextSelector,
)

from .const import (
    CONF_ENABLED, CONF_HEATER_ENTITY, CONF_HEATER_TYPE,
    CONF_MAX_POWER, CONF_POWER_SENSOR, CONF_POWER_W, CONF_PRIORITY,
    CONF_ROOM_ID, CONF_ROOM_NAME, CONF_ROOMS, CONF_TARGET_TEMP, CONF_TEMP_SENSOR,
    DEFAULT_MAX_POWER, DEFAULT_POWER_W, DEFAULT_PRIORITY, DEFAULT_TARGET_TEMP,
    DOMAIN, HEATER_TYPE_CLIMATE, HEATER_TYPE_SWITCH,
)

_LOGGER = logging.getLogger(__name__)


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", name.lower()).strip("_")


class PowerHeatingControllerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._global_config: dict = {}
        self._rooms: list[dict] = []
        self._current_room: dict = {}

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            if self.hass.states.get(user_input[CONF_POWER_SENSOR]) is None:
                errors[CONF_POWER_SENSOR] = "entity_not_found"
            else:
                self._global_config = user_input
                return await self.async_step_room_heater_type()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_POWER_SENSOR): EntitySelector(EntitySelectorConfig(domain="sensor")),
                vol.Required(CONF_MAX_POWER, default=DEFAULT_MAX_POWER): NumberSelector(
                    NumberSelectorConfig(min=500, max=20000, step=100, unit_of_measurement="W", mode=NumberSelectorMode.BOX)
                ),
            }),
            errors=errors,
        )

    async def async_step_room_heater_type(self, user_input=None):
        if user_input is not None:
            self._current_room = {
                CONF_ROOM_NAME: user_input[CONF_ROOM_NAME],
                CONF_ROOM_ID: _slugify(user_input[CONF_ROOM_NAME]),
                CONF_HEATER_TYPE: user_input[CONF_HEATER_TYPE],
            }
            return await self.async_step_room_heater_entity()

        return self.async_show_form(
            step_id="room_heater_type",
            data_schema=vol.Schema({
                vol.Required(CONF_ROOM_NAME): TextSelector(),
                vol.Required(CONF_HEATER_TYPE, default=HEATER_TYPE_CLIMATE): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            {"value": HEATER_TYPE_CLIMATE, "label": "Climate entity (e.g. AC / heat pump)"},
                            {"value": HEATER_TYPE_SWITCH, "label": "Switch (e.g. plug-in radiator)"},
                        ],
                        mode=SelectSelectorMode.LIST,
                    )
                ),
            }),
        )

    async def async_step_room_heater_entity(self, user_input=None):
        errors = {}
        if user_input is not None:
            if self.hass.states.get(user_input[CONF_HEATER_ENTITY]) is None:
                errors[CONF_HEATER_ENTITY] = "entity_not_found"
            if self.hass.states.get(user_input[CONF_TEMP_SENSOR]) is None:
                errors[CONF_TEMP_SENSOR] = "entity_not_found"
            if not errors:
                self._current_room.update(user_input)
                return await self.async_step_room_settings()

        heater_domain = "climate" if self._current_room.get(CONF_HEATER_TYPE) == HEATER_TYPE_CLIMATE else "switch"
        return self.async_show_form(
            step_id="room_heater_entity",
            data_schema=vol.Schema({
                vol.Required(CONF_HEATER_ENTITY): EntitySelector(EntitySelectorConfig(domain=heater_domain)),
                vol.Required(CONF_TEMP_SENSOR): EntitySelector(EntitySelectorConfig(domain="sensor", device_class="temperature")),
            }),
            errors=errors,
        )

    async def async_step_room_settings(self, user_input=None):
        if user_input is not None:
            self._current_room.update({
                CONF_TARGET_TEMP: user_input[CONF_TARGET_TEMP],
                CONF_POWER_W: int(user_input[CONF_POWER_W]),
                CONF_PRIORITY: int(user_input[CONF_PRIORITY]),
                CONF_ENABLED: True,
            })
            self._rooms.append(self._current_room)
            self._current_room = {}
            return await self.async_step_add_another()

        return self.async_show_form(
            step_id="room_settings",
            data_schema=vol.Schema({
                vol.Required(CONF_TARGET_TEMP, default=DEFAULT_TARGET_TEMP): NumberSelector(
                    NumberSelectorConfig(min=5, max=35, step=0.5, unit_of_measurement="°C", mode=NumberSelectorMode.SLIDER)
                ),
                vol.Required(CONF_POWER_W, default=DEFAULT_POWER_W): NumberSelector(
                    NumberSelectorConfig(min=100, max=5000, step=50, unit_of_measurement="W", mode=NumberSelectorMode.BOX)
                ),
                vol.Required(CONF_PRIORITY, default=DEFAULT_PRIORITY): NumberSelector(
                    NumberSelectorConfig(min=1, max=10, step=1, mode=NumberSelectorMode.BOX)
                ),
            }),
        )

    async def async_step_add_another(self, user_input=None):
        if user_input is not None:
            if user_input.get("add_another"):
                return await self.async_step_room_heater_type()
            return self._create_entry()

        return self.async_show_form(
            step_id="add_another",
            data_schema=vol.Schema({vol.Required("add_another", default=False): bool}),
            description_placeholders={
                "room_count": str(len(self._rooms)),
                "rooms": ", ".join(r[CONF_ROOM_NAME] for r in self._rooms),
            },
        )

    def _create_entry(self):
        return self.async_create_entry(
            title="Power Heating Controller",
            data={
                CONF_POWER_SENSOR: self._global_config[CONF_POWER_SENSOR],
                CONF_MAX_POWER: self._global_config[CONF_MAX_POWER],
                CONF_ROOMS: self._rooms,
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return PowerHeatingOptionsFlow(config_entry)


class PowerHeatingOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry
        self._options = dict(config_entry.options or config_entry.data)
        self._selected_room_id: str | None = None
        self._new_room: dict = {}

    async def async_step_init(self, user_input=None):
        rooms: list[dict] = self._options.get(CONF_ROOMS, [])
        actions = [{"value": "global", "label": "⚡ Edit power limit & sensor"}]
        actions += [{"value": r[CONF_ROOM_ID], "label": r[CONF_ROOM_NAME]} for r in rooms]
        actions.append({"value": "__add__", "label": "➕ Add new room"})

        if user_input is not None:
            action = user_input.get("action")
            if action == "global":
                return await self.async_step_global()
            elif action == "__add__":
                return await self.async_step_room_heater_type()
            elif action:
                self._selected_room_id = action
                return await self.async_step_edit_room()

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required("action"): SelectSelector(
                    SelectSelectorConfig(options=actions, mode=SelectSelectorMode.LIST)
                )
            }),
        )

    async def async_step_global(self, user_input=None):
        if user_input is not None:
            self._options[CONF_POWER_SENSOR] = user_input[CONF_POWER_SENSOR]
            self._options[CONF_MAX_POWER] = user_input[CONF_MAX_POWER]
            return self.async_create_entry(title="", data=self._options)

        return self.async_show_form(
            step_id="global",
            data_schema=vol.Schema({
                vol.Required(CONF_POWER_SENSOR, default=self._options.get(CONF_POWER_SENSOR)): EntitySelector(EntitySelectorConfig(domain="sensor")),
                vol.Required(CONF_MAX_POWER, default=self._options.get(CONF_MAX_POWER, DEFAULT_MAX_POWER)): NumberSelector(
                    NumberSelectorConfig(min=500, max=20000, step=100, unit_of_measurement="W", mode=NumberSelectorMode.BOX)
                ),
            }),
        )

    async def async_step_edit_room(self, user_input=None):
        rooms: list[dict] = self._options.get(CONF_ROOMS, [])
        room = next((r for r in rooms if r[CONF_ROOM_ID] == self._selected_room_id), None)
        if room is None:
            return await self.async_step_init()

        if user_input is not None:
            if user_input.get("delete_room"):
                self._options[CONF_ROOMS] = [r for r in rooms if r[CONF_ROOM_ID] != self._selected_room_id]
                return self.async_create_entry(title="", data=self._options)
            for key in (CONF_TARGET_TEMP, CONF_POWER_W, CONF_PRIORITY, CONF_ENABLED):
                if key in user_input:
                    room[key] = user_input[key]
            return self.async_create_entry(title="", data=self._options)

        return self.async_show_form(
            step_id="edit_room",
            data_schema=vol.Schema({
                vol.Required(CONF_TARGET_TEMP, default=room.get(CONF_TARGET_TEMP, DEFAULT_TARGET_TEMP)): NumberSelector(
                    NumberSelectorConfig(min=5, max=35, step=0.5, unit_of_measurement="°C", mode=NumberSelectorMode.SLIDER)
                ),
                vol.Required(CONF_POWER_W, default=room.get(CONF_POWER_W, DEFAULT_POWER_W)): NumberSelector(
                    NumberSelectorConfig(min=100, max=5000, step=50, unit_of_measurement="W", mode=NumberSelectorMode.BOX)
                ),
                vol.Required(CONF_PRIORITY, default=room.get(CONF_PRIORITY, DEFAULT_PRIORITY)): NumberSelector(
                    NumberSelectorConfig(min=1, max=10, step=1, mode=NumberSelectorMode.BOX)
                ),
                vol.Required(CONF_ENABLED, default=room.get(CONF_ENABLED, True)): bool,
                vol.Optional("delete_room", default=False): bool,
            }),
            description_placeholders={"room_name": room[CONF_ROOM_NAME]},
        )

    async def async_step_room_heater_type(self, user_input=None):
        if user_input is not None:
            self._new_room = {
                CONF_ROOM_NAME: user_input[CONF_ROOM_NAME],
                CONF_ROOM_ID: _slugify(user_input[CONF_ROOM_NAME]),
                CONF_HEATER_TYPE: user_input[CONF_HEATER_TYPE],
            }
            return await self.async_step_room_heater_entity()

        return self.async_show_form(
            step_id="room_heater_type",
            data_schema=vol.Schema({
                vol.Required(CONF_ROOM_NAME): TextSelector(),
                vol.Required(CONF_HEATER_TYPE, default=HEATER_TYPE_CLIMATE): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            {"value": HEATER_TYPE_CLIMATE, "label": "Climate entity"},
                            {"value": HEATER_TYPE_SWITCH, "label": "Switch"},
                        ],
                        mode=SelectSelectorMode.LIST,
                    )
                ),
            }),
        )

    async def async_step_room_heater_entity(self, user_input=None):
        errors = {}
        if user_input is not None:
            if self.hass.states.get(user_input[CONF_HEATER_ENTITY]) is None:
                errors[CONF_HEATER_ENTITY] = "entity_not_found"
            if self.hass.states.get(user_input[CONF_TEMP_SENSOR]) is None:
                errors[CONF_TEMP_SENSOR] = "entity_not_found"
            if not errors:
                self._new_room.update(user_input)
                return await self.async_step_room_settings()

        heater_domain = "climate" if self._new_room.get(CONF_HEATER_TYPE) == HEATER_TYPE_CLIMATE else "switch"
        return self.async_show_form(
            step_id="room_heater_entity",
            data_schema=vol.Schema({
                vol.Required(CONF_HEATER_ENTITY): EntitySelector(EntitySelectorConfig(domain=heater_domain)),
                vol.Required(CONF_TEMP_SENSOR): EntitySelector(EntitySelectorConfig(domain="sensor", device_class="temperature")),
            }),
            errors=errors,
        )

    async def async_step_room_settings(self, user_input=None):
        if user_input is not None:
            self._new_room.update({
                CONF_TARGET_TEMP: user_input[CONF_TARGET_TEMP],
                CONF_POWER_W: int(user_input[CONF_POWER_W]),
                CONF_PRIORITY: int(user_input[CONF_PRIORITY]),
                CONF_ENABLED: True,
            })
            rooms = list(self._options.get(CONF_ROOMS, []))
            rooms.append(self._new_room)
            self._options[CONF_ROOMS] = rooms
            return self.async_create_entry(title="", data=self._options)

        return self.async_show_form(
            step_id="room_settings",
            data_schema=vol.Schema({
                vol.Required(CONF_TARGET_TEMP, default=DEFAULT_TARGET_TEMP): NumberSelector(
                    NumberSelectorConfig(min=5, max=35, step=0.5, unit_of_measurement="°C", mode=NumberSelectorMode.SLIDER)
                ),
                vol.Required(CONF_POWER_W, default=DEFAULT_POWER_W): NumberSelector(
                    NumberSelectorConfig(min=100, max=5000, step=50, unit_of_measurement="W", mode=NumberSelectorMode.BOX)
                ),
                vol.Required(CONF_PRIORITY, default=DEFAULT_PRIORITY): NumberSelector(
                    NumberSelectorConfig(min=1, max=10, step=1, mode=NumberSelectorMode.BOX)
                ),
            }),
        )