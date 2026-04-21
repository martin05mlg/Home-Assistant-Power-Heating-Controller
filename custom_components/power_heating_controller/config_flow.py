"""Config flow for Power Heating Controller."""

from __future__ import annotations

import logging
import re
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.selector import (
    BooleanSelector,          # ← added; replaces bare `bool` in schemas
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)

from .const import (
    CONF_ENABLED,
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
    HEATER_TYPE_SWITCH,
)

_LOGGER = logging.getLogger(__name__)


def _slugify(name: str) -> str:
    """Convert a room name to a safe room_id slug."""
    return re.sub(r"[^a-z0-9_]", "_", name.lower()).strip("_")


# ── Main config flow ──────────────────────────────────────────────────────────

class PowerHeatingControllerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the initial setup: power sensor + max power, then add rooms."""

    VERSION = 1

    def __init__(self) -> None:
        self._global_config: dict = {}
        self._rooms: list[dict] = []
        self._current_room: dict = {}

    # Step 1 — global settings (power sensor + limit)
    async def async_step_user(self, user_input=None):
        errors = {}

        if user_input is not None:
            state = self.hass.states.get(user_input[CONF_POWER_SENSOR])
            if state is None:
                errors[CONF_POWER_SENSOR] = "entity_not_found"
            else:
                self._global_config = user_input
                return await self.async_step_room_heater_type()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_POWER_SENSOR): EntitySelector(
                        EntitySelectorConfig(domain="sensor")
                    ),
                    vol.Required(CONF_MAX_POWER, default=DEFAULT_MAX_POWER): NumberSelector(
                        NumberSelectorConfig(min=500, max=20000, step=100, unit_of_measurement="W", mode=NumberSelectorMode.BOX)
                    ),
                }
            ),
            errors=errors,
            description_placeholders={
                "info": "Select the sensor that reports your total household power consumption."
            },
        )

    # Step 2 — room: basic info + heater type
    async def async_step_room_heater_type(self, user_input=None):
        errors = {}

        if user_input is not None:
            self._current_room = {
                CONF_ROOM_NAME: user_input[CONF_ROOM_NAME],
                CONF_ROOM_ID: _slugify(user_input[CONF_ROOM_NAME]),
                CONF_HEATER_TYPE: user_input[CONF_HEATER_TYPE],
            }
            return await self.async_step_room_heater_entity()

        return self.async_show_form(
            step_id="room_heater_type",
            data_schema=vol.Schema(
                {
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
                }
            ),
            errors=errors,
        )

    # Step 3 — room: heater entity + temp sensor
    async def async_step_room_heater_entity(self, user_input=None):
        errors = {}

        if user_input is not None:
            heater_entity = user_input[CONF_HEATER_ENTITY]
            temp_sensor = user_input[CONF_TEMP_SENSOR]

            if self.hass.states.get(heater_entity) is None:
                errors[CONF_HEATER_ENTITY] = "entity_not_found"
            if self.hass.states.get(temp_sensor) is None:
                errors[CONF_TEMP_SENSOR] = "entity_not_found"

            if not errors:
                self._current_room.update(
                    {
                        CONF_HEATER_ENTITY: heater_entity,
                        CONF_TEMP_SENSOR: temp_sensor,
                    }
                )
                return await self.async_step_room_settings()

        heater_domain = (
            "climate"
            if self._current_room.get(CONF_HEATER_TYPE) == HEATER_TYPE_CLIMATE
            else "switch"
        )

        return self.async_show_form(
            step_id="room_heater_entity",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HEATER_ENTITY): EntitySelector(
                        EntitySelectorConfig(domain=heater_domain)
                    ),
                    vol.Required(CONF_TEMP_SENSOR): EntitySelector(
                        EntitySelectorConfig(domain="sensor", device_class="temperature")
                    ),
                }
            ),
            errors=errors,
        )

    # Step 4 — room: power, target temp, priority
    async def async_step_room_settings(self, user_input=None):
        errors = {}

        if user_input is not None:
            self._current_room.update(
                {
                    CONF_TARGET_TEMP: float(user_input[CONF_TARGET_TEMP]),
                    CONF_POWER_W: int(user_input[CONF_POWER_W]),
                    CONF_PRIORITY: int(user_input[CONF_PRIORITY]),
                    CONF_ENABLED: True,
                }
            )
            self._rooms.append(self._current_room)
            self._current_room = {}
            return await self.async_step_add_another()

        return self.async_show_form(
            step_id="room_settings",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TARGET_TEMP, default=DEFAULT_TARGET_TEMP): NumberSelector(
                        NumberSelectorConfig(min=5, max=35, step=0.5, unit_of_measurement="°C", mode=NumberSelectorMode.SLIDER)
                    ),
                    vol.Required(CONF_POWER_W, default=DEFAULT_POWER_W): NumberSelector(
                        NumberSelectorConfig(min=100, max=5000, step=50, unit_of_measurement="W", mode=NumberSelectorMode.BOX)
                    ),
                    vol.Required(CONF_PRIORITY, default=DEFAULT_PRIORITY): NumberSelector(
                        NumberSelectorConfig(min=1, max=10, step=1, mode=NumberSelectorMode.BOX)
                    ),
                }
            ),
            description_placeholders={
                "info": "Priority: 1 = highest (last to lose power). 10 = lowest (first to be preempted)."
            },
            errors=errors,
        )

    # Step 5 — add another room or finish
    async def async_step_add_another(self, user_input=None):
        if user_input is not None:
            if user_input.get("add_another"):
                return await self.async_step_room_heater_type()
            else:
                return self._create_entry()

        room_count = len(self._rooms)
        return self.async_show_form(
            step_id="add_another",
            data_schema=vol.Schema(
                # FIX: BooleanSelector instead of bare bool
                {vol.Required("add_another", default=False): BooleanSelector()}
            ),
            description_placeholders={
                "room_count": str(room_count),
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

    # ── Options flow ──────────────────────────────────────────────────────────

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        # FIX: Do NOT pass config_entry to constructor.
        # HA 2024.11+ sets self.config_entry automatically on the flow instance.
        return PowerHeatingOptionsFlow()


# ── Options flow ──────────────────────────────────────────────────────────────

class PowerHeatingOptionsFlow(config_entries.OptionsFlow):
    """Allow editing max power and per-room settings after initial setup."""

    # FIX: Removed config_entry parameter from __init__.
    # HA 2024.11+ deprecated passing config_entry here; it is now available
    # as self.config_entry (set automatically before async_step_init is called).
    def __init__(self) -> None:
        self._options: dict = {}
        self._selected_room_id: str | None = None
        self._new_room: dict = {}

    async def async_step_init(self, user_input=None):
        """Main options menu. Lazy-init _options so self.config_entry is ready."""
        # FIX: initialise here where self.config_entry is guaranteed to be set.
        if not self._options:
            self._options = dict(self.config_entry.options or self.config_entry.data)

        rooms: list[dict] = self._options.get(CONF_ROOMS, [])
        room_options = [
            {"value": r[CONF_ROOM_ID], "label": r[CONF_ROOM_NAME]} for r in rooms
        ]
        room_options.append({"value": "__add__", "label": "➕ Add new room"})

        if user_input is not None:
            action = user_input.get("action")

            if action == "global":
                return await self.async_step_global()
            elif action == "__add__":
                return await self.async_step_room_heater_type()
            elif action:
                self._selected_room_id = action
                return await self.async_step_edit_room()

        actions = [{"value": "global", "label": "⚡ Edit power limit & sensor"}]
        actions += room_options

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required("action"): SelectSelector(
                        SelectSelectorConfig(options=actions, mode=SelectSelectorMode.LIST)
                    )
                }
            ),
        )

    async def async_step_global(self, user_input=None):
        if user_input is not None:
            self._options[CONF_POWER_SENSOR] = user_input[CONF_POWER_SENSOR]
            self._options[CONF_MAX_POWER] = float(user_input[CONF_MAX_POWER])
            return self.async_create_entry(title="", data=self._options)

        return self.async_show_form(
            step_id="global",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_POWER_SENSOR, default=self._options.get(CONF_POWER_SENSOR)): EntitySelector(
                        EntitySelectorConfig(domain="sensor")
                    ),
                    vol.Required(CONF_MAX_POWER, default=self._options.get(CONF_MAX_POWER, DEFAULT_MAX_POWER)): NumberSelector(
                        NumberSelectorConfig(min=500, max=20000, step=100, unit_of_measurement="W", mode=NumberSelectorMode.BOX)
                    ),
                }
            ),
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

            # FIX: cast numeric fields to correct types (NumberSelector returns float)
            if CONF_TARGET_TEMP in user_input:
                room[CONF_TARGET_TEMP] = float(user_input[CONF_TARGET_TEMP])
            if CONF_POWER_W in user_input:
                room[CONF_POWER_W] = int(user_input[CONF_POWER_W])
            if CONF_PRIORITY in user_input:
                room[CONF_PRIORITY] = int(user_input[CONF_PRIORITY])
            if CONF_ENABLED in user_input:
                room[CONF_ENABLED] = bool(user_input[CONF_ENABLED])
            if CONF_HVAC_MODE_HEAT in user_input:
                room[CONF_HVAC_MODE_HEAT] = user_input[CONF_HVAC_MODE_HEAT]

            return self.async_create_entry(title="", data=self._options)

        return self.async_show_form(
            step_id="edit_room",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TARGET_TEMP, default=room.get(CONF_TARGET_TEMP, DEFAULT_TARGET_TEMP)): NumberSelector(
                        NumberSelectorConfig(min=5, max=35, step=0.5, unit_of_measurement="°C", mode=NumberSelectorMode.SLIDER)
                    ),
                    vol.Required(CONF_POWER_W, default=room.get(CONF_POWER_W, DEFAULT_POWER_W)): NumberSelector(
                        NumberSelectorConfig(min=100, max=5000, step=50, unit_of_measurement="W", mode=NumberSelectorMode.BOX)
                    ),
                    vol.Required(CONF_PRIORITY, default=room.get(CONF_PRIORITY, DEFAULT_PRIORITY)): NumberSelector(
                        NumberSelectorConfig(min=1, max=10, step=1, mode=NumberSelectorMode.BOX)
                    ),
                    # FIX: BooleanSelector instead of bare bool — bare bool breaks HA's form renderer
                    vol.Required(CONF_ENABLED, default=room.get(CONF_ENABLED, True)): BooleanSelector(),
                    vol.Optional("delete_room", default=False): BooleanSelector(),
                    # Added: expose hvac_mode_heat so non-standard climate modes (e.g. Tuya) can be set
                    vol.Optional(CONF_HVAC_MODE_HEAT, default=room.get(CONF_HVAC_MODE_HEAT, DEFAULT_HVAC_MODE_HEAT)): TextSelector(),
                }
            ),
            description_placeholders={"room_name": room[CONF_ROOM_NAME]},
        )

    # ── Add room steps (reused in options flow) ───────────────────────────────

    async def async_step_room_heater_type(self, user_input=None):
        errors = {}
        if user_input is not None:
            self._new_room = {
                CONF_ROOM_NAME: user_input[CONF_ROOM_NAME],
                CONF_ROOM_ID: _slugify(user_input[CONF_ROOM_NAME]),
                CONF_HEATER_TYPE: user_input[CONF_HEATER_TYPE],
            }
            return await self.async_step_room_heater_entity()

        return self.async_show_form(
            step_id="room_heater_type",
            data_schema=vol.Schema(
                {
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
                }
            ),
            errors=errors,
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

        heater_domain = (
            "climate" if self._new_room.get(CONF_HEATER_TYPE) == HEATER_TYPE_CLIMATE else "switch"
        )
        return self.async_show_form(
            step_id="room_heater_entity",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HEATER_ENTITY): EntitySelector(EntitySelectorConfig(domain=heater_domain)),
                    vol.Required(CONF_TEMP_SENSOR): EntitySelector(EntitySelectorConfig(domain="sensor", device_class="temperature")),
                }
            ),
            errors=errors,
        )

    async def async_step_room_settings(self, user_input=None):
        if user_input is not None:
            self._new_room.update(
                {
                    CONF_TARGET_TEMP: float(user_input[CONF_TARGET_TEMP]),
                    CONF_POWER_W: int(user_input[CONF_POWER_W]),
                    CONF_PRIORITY: int(user_input[CONF_PRIORITY]),
                    CONF_ENABLED: True,
                }
            )
            rooms = list(self._options.get(CONF_ROOMS, []))
            rooms.append(self._new_room)
            self._options[CONF_ROOMS] = rooms
            return self.async_create_entry(title="", data=self._options)

        return self.async_show_form(
            step_id="room_settings",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TARGET_TEMP, default=DEFAULT_TARGET_TEMP): NumberSelector(
                        NumberSelectorConfig(min=5, max=35, step=0.5, unit_of_measurement="°C", mode=NumberSelectorMode.SLIDER)
                    ),
                    vol.Required(CONF_POWER_W, default=DEFAULT_POWER_W): NumberSelector(
                        NumberSelectorConfig(min=100, max=5000, step=50, unit_of_measurement="W", mode=NumberSelectorMode.BOX)
                    ),
                    vol.Required(CONF_PRIORITY, default=DEFAULT_PRIORITY): NumberSelector(
                        NumberSelectorConfig(min=1, max=10, step=1, mode=NumberSelectorMode.BOX)
                    ),
                }
            ),
        )
