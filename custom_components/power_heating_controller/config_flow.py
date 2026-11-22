"""Config and options flow for Power Heating Controller."""

from __future__ import annotations

import copy
from typing import Any

import voluptuous as vol

from homeassistant.components.climate import ATTR_HVAC_MODES, HVACMode
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.selector import (
    BooleanSelector,
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
    DEFAULT_COLD_TOLERANCE,
    DEFAULT_FORCE_MAX_TEMP,
    DEFAULT_HOT_TOLERANCE,
    DEFAULT_HVAC_MODE_HEAT,
    DEFAULT_MAX_POWER,
    DEFAULT_POWER_W,
    DEFAULT_PRIORITY,
    DEFAULT_TARGET_TEMP,
    DOMAIN,
    HEATER_TYPE_CLIMATE,
    HEATER_TYPES,
    MAX_MAX_POWER,
    MAX_PRIORITY,
    MAX_ROOM_POWER,
    MAX_TARGET_TEMP,
    MIN_MAX_POWER,
    MIN_PRIORITY,
    MIN_ROOM_POWER,
    MIN_TARGET_TEMP,
    TARGET_TEMP_STEP,
)
from .helpers import make_room_id

ACTION_GLOBAL = "__global__"
ACTION_ADD = "__add__"
DELETE_ROOM = "delete_room"
ADD_ANOTHER = "add_another"

NON_HEATING_MODES = {HVACMode.OFF, HVACMode.COOL, HVACMode.DRY, HVACMode.FAN_ONLY}


# ── Schemas ──────────────────────────────────────────────────────────────────

def _global_schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_POWER_SENSOR, default=defaults.get(CONF_POWER_SENSOR, vol.UNDEFINED)): EntitySelector(
                EntitySelectorConfig(domain="sensor")
            ),
            vol.Required(CONF_MAX_POWER, default=defaults.get(CONF_MAX_POWER, DEFAULT_MAX_POWER)): NumberSelector(
                NumberSelectorConfig(
                    min=MIN_MAX_POWER, max=MAX_MAX_POWER, step=100,
                    unit_of_measurement="W", mode=NumberSelectorMode.BOX,
                )
            ),
            vol.Required(CONF_COLD_TOLERANCE, default=defaults.get(CONF_COLD_TOLERANCE, DEFAULT_COLD_TOLERANCE)): NumberSelector(
                NumberSelectorConfig(min=0, max=3, step=0.1, unit_of_measurement="°C", mode=NumberSelectorMode.BOX)
            ),
            vol.Required(CONF_HOT_TOLERANCE, default=defaults.get(CONF_HOT_TOLERANCE, DEFAULT_HOT_TOLERANCE)): NumberSelector(
                NumberSelectorConfig(min=0, max=3, step=0.1, unit_of_measurement="°C", mode=NumberSelectorMode.BOX)
            ),
        }
    )


def _global_from_input(user_input: dict[str, Any]) -> dict[str, Any]:
    return {
        CONF_POWER_SENSOR: user_input[CONF_POWER_SENSOR],
        CONF_MAX_POWER: float(user_input[CONF_MAX_POWER]),
        CONF_COLD_TOLERANCE: float(user_input[CONF_COLD_TOLERANCE]),
        CONF_HOT_TOLERANCE: float(user_input[CONF_HOT_TOLERANCE]),
    }


def _heater_domain(heater_type: str | None) -> str:
    return "climate" if heater_type == HEATER_TYPE_CLIMATE else "switch"


def _room_name_schema(default_name: str | None = None) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_ROOM_NAME, default=default_name or vol.UNDEFINED): TextSelector(),
            vol.Required(CONF_HEATER_TYPE, default=HEATER_TYPE_CLIMATE): SelectSelector(
                SelectSelectorConfig(
                    options=HEATER_TYPES,
                    mode=SelectSelectorMode.LIST,
                    translation_key="heater_type",
                )
            ),
        }
    )


def _room_entities_fields(heater_type: str, room: dict[str, Any]) -> dict:
    return {
        vol.Required(CONF_HEATER_ENTITY, default=room.get(CONF_HEATER_ENTITY, vol.UNDEFINED)): EntitySelector(
            EntitySelectorConfig(domain=_heater_domain(heater_type))
        ),
        vol.Required(CONF_TEMP_SENSOR, default=room.get(CONF_TEMP_SENSOR, vol.UNDEFINED)): EntitySelector(
            EntitySelectorConfig(domain="sensor", device_class="temperature")
        ),
    }


def _heating_modes(hass: HomeAssistant, entity_id: str) -> list[str] | None:
    """Return advertised heating modes or None if the device has no mode data."""
    state = hass.states.get(entity_id)
    modes = state.attributes.get(ATTR_HVAC_MODES) if state else None
    if not isinstance(modes, (list, tuple)) or not modes:
        return None
    eligible = (
        mode for mode in modes
        if isinstance(mode, str) and mode and mode not in NON_HEATING_MODES
    )
    return list(dict.fromkeys(eligible))


def _heating_mode_field(modes: list[str], saved_mode: str | None = None) -> dict:
    """Build a selector that stores the raw HVAC mode advertised by the heater."""
    default = DEFAULT_HVAC_MODE_HEAT if DEFAULT_HVAC_MODE_HEAT in modes else modes[0]
    if saved_mode in modes:
        default = saved_mode
    return {
        vol.Required(CONF_HVAC_MODE_HEAT, default=default): SelectSelector(
            SelectSelectorConfig(
                options=modes,
                mode=SelectSelectorMode.DROPDOWN,
                translation_key="heating_mode",
            )
        )
    }


def _room_settings_fields(
    heater_type: str, room: dict[str, Any], modes: list[str] | None = None
) -> dict:
    fields: dict = {
        vol.Required(CONF_TARGET_TEMP, default=room.get(CONF_TARGET_TEMP, DEFAULT_TARGET_TEMP)): NumberSelector(
            NumberSelectorConfig(
                min=MIN_TARGET_TEMP, max=MAX_TARGET_TEMP, step=TARGET_TEMP_STEP,
                unit_of_measurement="°C", mode=NumberSelectorMode.SLIDER,
            )
        ),
        vol.Required(CONF_POWER_W, default=room.get(CONF_POWER_W, DEFAULT_POWER_W)): NumberSelector(
            NumberSelectorConfig(
                min=MIN_ROOM_POWER, max=MAX_ROOM_POWER, step=50,
                unit_of_measurement="W", mode=NumberSelectorMode.BOX,
            )
        ),
        vol.Required(CONF_PRIORITY, default=room.get(CONF_PRIORITY, DEFAULT_PRIORITY)): NumberSelector(
            NumberSelectorConfig(min=MIN_PRIORITY, max=MAX_PRIORITY, step=1, mode=NumberSelectorMode.BOX)
        ),
    }
    if heater_type == HEATER_TYPE_CLIMATE:
        if modes:
            fields.update(_heating_mode_field(modes, room.get(CONF_HVAC_MODE_HEAT)))
        fields[vol.Required(CONF_FORCE_MAX_TEMP, default=room.get(CONF_FORCE_MAX_TEMP, DEFAULT_FORCE_MAX_TEMP))] = BooleanSelector()
    return fields


def _apply_room_settings(room: dict[str, Any], user_input: dict[str, Any]) -> None:
    room[CONF_TARGET_TEMP] = float(user_input[CONF_TARGET_TEMP])
    room[CONF_POWER_W] = int(user_input[CONF_POWER_W])
    room[CONF_PRIORITY] = int(user_input[CONF_PRIORITY])
    if room.get(CONF_HEATER_TYPE) == HEATER_TYPE_CLIMATE:
        if CONF_HVAC_MODE_HEAT in user_input:
            room[CONF_HVAC_MODE_HEAT] = user_input[CONF_HVAC_MODE_HEAT]
        room[CONF_FORCE_MAX_TEMP] = bool(user_input.get(CONF_FORCE_MAX_TEMP, DEFAULT_FORCE_MAX_TEMP))


# ── Shared "add a room" steps ────────────────────────────────────────────────

class _AddRoomStepsMixin:
    """Steps room_heater_type → room_heater_entity → room_settings.

    Subclasses provide ``_existing_room_ids`` and ``_async_room_added``.
    """

    _new_room: dict[str, Any]

    def _existing_room_ids(self) -> set[str]:
        raise NotImplementedError

    async def _async_room_added(self, room: dict[str, Any]) -> ConfigFlowResult:
        raise NotImplementedError

    async def async_step_room_heater_type(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            name = user_input[CONF_ROOM_NAME].strip()
            room_id = make_room_id(name)
            if not room_id:
                errors[CONF_ROOM_NAME] = "invalid_name"
            elif room_id in self._existing_room_ids():
                errors[CONF_ROOM_NAME] = "room_exists"
            else:
                self._new_room = {
                    CONF_ROOM_ID: room_id,
                    CONF_ROOM_NAME: name,
                    CONF_HEATER_TYPE: user_input[CONF_HEATER_TYPE],
                }
                return await self.async_step_room_heater_entity()

        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="room_heater_type",
            data_schema=_room_name_schema(user_input.get(CONF_ROOM_NAME) if user_input else None),
            errors=errors,
        )

    async def async_step_room_heater_entity(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            heater = user_input[CONF_HEATER_ENTITY]
            if self.hass.states.get(heater) is None:  # type: ignore[attr-defined]
                errors[CONF_HEATER_ENTITY] = "entity_not_found"
            elif (
                self._new_room[CONF_HEATER_TYPE] == HEATER_TYPE_CLIMATE
                and not _heating_modes(self.hass, heater)
            ):
                errors[CONF_HEATER_ENTITY] = "no_heating_modes"
            if self.hass.states.get(user_input[CONF_TEMP_SENSOR]) is None:  # type: ignore[attr-defined]
                errors[CONF_TEMP_SENSOR] = "entity_not_found"
            if not errors:
                self._new_room[CONF_HEATER_ENTITY] = user_input[CONF_HEATER_ENTITY]
                self._new_room[CONF_TEMP_SENSOR] = user_input[CONF_TEMP_SENSOR]
                return await self.async_step_room_settings()

        heater_type = self._new_room[CONF_HEATER_TYPE]
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="room_heater_entity",
            data_schema=vol.Schema(_room_entities_fields(heater_type, user_input or {})),
            errors=errors,
            description_placeholders={"room_name": self._new_room[CONF_ROOM_NAME]},
        )

    async def async_step_room_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        room = self._new_room
        heater_type = room[CONF_HEATER_TYPE]
        modes = None
        errors: dict[str, str] = {}
        if heater_type == HEATER_TYPE_CLIMATE:
            modes = _heating_modes(self.hass, room[CONF_HEATER_ENTITY])
            if not modes:
                return await self.async_step_room_heater_entity({
                    CONF_HEATER_ENTITY: room[CONF_HEATER_ENTITY],
                    CONF_TEMP_SENSOR: room[CONF_TEMP_SENSOR],
                })
            if user_input is not None and user_input.get(CONF_HVAC_MODE_HEAT) not in modes:
                errors[CONF_HVAC_MODE_HEAT] = "invalid_heating_mode"
        if user_input is not None and not errors:
            _apply_room_settings(room, user_input)
            room[CONF_ENABLED] = True
            self._new_room = {}
            return await self._async_room_added(room)

        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="room_settings",
            data_schema=vol.Schema(_room_settings_fields(heater_type, user_input or {}, modes)),
            errors=errors,
            description_placeholders={"room_name": room[CONF_ROOM_NAME]},
        )


# ── Config flow ──────────────────────────────────────────────────────────────

class PowerHeatingControllerConfigFlow(_AddRoomStepsMixin, ConfigFlow, domain=DOMAIN):
    """Initial setup: global settings, then rooms one by one."""

    VERSION = 1

    def __init__(self) -> None:
        self._global_config: dict[str, Any] = {}
        self._rooms: list[dict[str, Any]] = []
        self._new_room = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        errors: dict[str, str] = {}
        if user_input is not None:
            if self.hass.states.get(user_input[CONF_POWER_SENSOR]) is None:
                errors[CONF_POWER_SENSOR] = "entity_not_found"
            else:
                self._global_config = _global_from_input(user_input)
                return await self.async_step_room_heater_type()

        return self.async_show_form(
            step_id="user", data_schema=_global_schema(user_input or {}), errors=errors
        )

    def _existing_room_ids(self) -> set[str]:
        return {r[CONF_ROOM_ID] for r in self._rooms}

    async def _async_room_added(self, room: dict[str, Any]) -> ConfigFlowResult:
        self._rooms.append(room)
        return await self.async_step_add_another()

    async def async_step_add_another(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            if user_input.get(ADD_ANOTHER):
                return await self.async_step_room_heater_type()
            data = {**self._global_config, CONF_ROOMS: self._rooms}
            return self.async_create_entry(
                title="Power Heating Controller",
                data=data,
                options=copy.deepcopy(data),
            )

        return self.async_show_form(
            step_id="add_another",
            data_schema=vol.Schema({vol.Required(ADD_ANOTHER, default=False): BooleanSelector()}),
            description_placeholders={
                "room_count": str(len(self._rooms)),
                "rooms": ", ".join(r[CONF_ROOM_NAME] for r in self._rooms),
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return PowerHeatingOptionsFlow()


# ── Options flow ─────────────────────────────────────────────────────────────

class PowerHeatingOptionsFlow(_AddRoomStepsMixin, OptionsFlow):
    """Edit global settings, edit/delete rooms or add new ones."""

    def __init__(self) -> None:
        self._options: dict[str, Any] = {}
        self._selected_room_id: str | None = None
        self._edited_room: dict[str, Any] = {}
        self._new_room = {}

    def _rooms(self) -> list[dict[str, Any]]:
        return self._options.setdefault(CONF_ROOMS, [])

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if not self._options:
            # Deep copy: never mutate the live entry options in place, otherwise
            # HA sees "no change" and neither saves nor reloads.
            self._options = copy.deepcopy(
                dict(self.config_entry.options or self.config_entry.data)
            )

        if user_input is not None:
            action = user_input["action"]
            if action == ACTION_GLOBAL:
                return await self.async_step_global()
            if action == ACTION_ADD:
                return await self.async_step_room_heater_type()
            self._selected_room_id = action
            return await self.async_step_edit_room()

        actions = [{"value": ACTION_GLOBAL, "label": "Power limit, sensor and tolerances"}]
        actions.extend(
            {"value": r[CONF_ROOM_ID], "label": r[CONF_ROOM_NAME]} for r in self._rooms()
        )
        actions.append({"value": ACTION_ADD, "label": "Add room"})

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required("action"): SelectSelector(
                        SelectSelectorConfig(
                            options=actions,
                            mode=SelectSelectorMode.LIST,
                            translation_key="room_action",
                        )
                    )
                }
            ),
        )

    async def async_step_global(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if self.hass.states.get(user_input[CONF_POWER_SENSOR]) is None:
                errors[CONF_POWER_SENSOR] = "entity_not_found"
            else:
                self._options.update(_global_from_input(user_input))
                return self.async_create_entry(title="", data=self._options)

        return self.async_show_form(
            step_id="global",
            data_schema=_global_schema(user_input or self._options),
            errors=errors,
        )

    async def async_step_edit_room(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        room = self._selected_room()
        if room is None:
            return self.async_abort(reason="room_not_found")

        heater_type = room.get(CONF_HEATER_TYPE, HEATER_TYPE_CLIMATE)
        modes = None
        if heater_type == HEATER_TYPE_CLIMATE:
            modes = _heating_modes(self.hass, room[CONF_HEATER_ENTITY])
            saved_mode = room.get(CONF_HVAC_MODE_HEAT, DEFAULT_HVAC_MODE_HEAT)
            if modes is None and saved_mode and saved_mode not in NON_HEATING_MODES:
                modes = [saved_mode]

        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get(DELETE_ROOM):
                return await self.async_step_confirm_remove_room()

            name = user_input[CONF_ROOM_NAME].strip()
            heater = user_input[CONF_HEATER_ENTITY]
            changing_climate = (
                heater_type == HEATER_TYPE_CLIMATE and heater != room[CONF_HEATER_ENTITY]
            )
            if not name:
                errors[CONF_ROOM_NAME] = "invalid_name"
            if self.hass.states.get(heater) is None:
                errors[CONF_HEATER_ENTITY] = "entity_not_found"
            elif changing_climate:
                if not _heating_modes(self.hass, heater):
                    errors[CONF_HEATER_ENTITY] = "no_heating_modes"
            elif modes and user_input.get(CONF_HVAC_MODE_HEAT) not in modes:
                errors[CONF_HVAC_MODE_HEAT] = "invalid_heating_mode"
            if self.hass.states.get(user_input[CONF_TEMP_SENSOR]) is None:
                errors[CONF_TEMP_SENSOR] = "entity_not_found"

            if not errors:
                # Keep the room ID so its entity IDs and history are preserved.
                self._edited_room = {
                    **room,
                    CONF_ROOM_NAME: name,
                    CONF_HEATER_ENTITY: heater,
                    CONF_TEMP_SENSOR: user_input[CONF_TEMP_SENSOR],
                    CONF_ENABLED: bool(user_input[CONF_ENABLED]),
                }
                settings = dict(user_input)
                if changing_climate or not modes:
                    settings.pop(CONF_HVAC_MODE_HEAT, None)
                _apply_room_settings(self._edited_room, settings)
                if changing_climate:
                    return await self.async_step_edit_room_hvac()
                result, save_error = await self._async_save_edited_room()
                if result is not None:
                    return result
                errors["base"] = save_error

        defaults = {**room, **(user_input or {})}
        schema: dict = {
            vol.Required(CONF_ROOM_NAME, default=defaults.get(CONF_ROOM_NAME, "")): TextSelector(),
            **_room_entities_fields(heater_type, defaults),
            **_room_settings_fields(heater_type, defaults, modes),
            vol.Required(CONF_ENABLED, default=defaults.get(CONF_ENABLED, True)): BooleanSelector(),
            vol.Optional(DELETE_ROOM, default=False): BooleanSelector(),
        }
        return self.async_show_form(
            step_id="edit_room",
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders={"room_name": room[CONF_ROOM_NAME], "room_id": room[CONF_ROOM_ID]},
        )

    async def async_step_edit_room_hvac(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        room = self._edited_room
        modes = _heating_modes(self.hass, room[CONF_HEATER_ENTITY])
        if not modes:
            return await self.async_step_edit_room(room)

        errors = {}
        if user_input is not None:
            mode = user_input[CONF_HVAC_MODE_HEAT]
            if mode in modes:
                room[CONF_HVAC_MODE_HEAT] = mode
                result, save_error = await self._async_save_edited_room()
                if result is not None:
                    return result
                errors["base"] = save_error
            else:
                errors[CONF_HVAC_MODE_HEAT] = "invalid_heating_mode"
        return self.async_show_form(
            step_id="edit_room_hvac",
            data_schema=vol.Schema(_heating_mode_field(modes, (user_input or {}).get(CONF_HVAC_MODE_HEAT))),
            errors=errors,
            description_placeholders={"room_name": room[CONF_ROOM_NAME]},
        )

    async def async_step_confirm_remove_room(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        room = self._selected_room()
        if room is None:
            return self.async_abort(reason="room_not_found")

        errors = {}
        if user_input is not None:
            entry = self.config_entry
            if entry.state is not ConfigEntryState.LOADED:
                errors["base"] = "not_loaded"
            else:
                try:
                    await entry.runtime_data.async_remove_room(room[CONF_ROOM_ID])
                except ServiceValidationError as err:
                    if err.translation_key == "room_not_found":
                        return self.async_abort(reason="room_not_found")
                    if err.translation_key == "room_changed":
                        return self.async_abort(reason="room_changed")
                    errors["base"] = "heater_shutdown_failed"
                else:
                    return self.async_create_entry(title="", data=dict(entry.options))

        return self.async_show_form(
            step_id="confirm_remove_room",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={"room_name": room[CONF_ROOM_NAME]},
        )

    def _selected_room(self) -> dict[str, Any] | None:
        return next((room for room in self._rooms() if room[CONF_ROOM_ID] == self._selected_room_id), None)

    async def _async_save_edited_room(self) -> tuple[ConfigFlowResult | None, str]:
        original = self._selected_room()
        if original is None:
            return self.async_abort(reason="room_not_found"), ""

        changes = {
            key: value for key, value in self._edited_room.items() if original.get(key) != value
        }
        if CONF_HEATER_ENTITY in changes and CONF_HVAC_MODE_HEAT in self._edited_room:
            changes[CONF_HVAC_MODE_HEAT] = self._edited_room[CONF_HVAC_MODE_HEAT]

        entry = self.config_entry
        if CONF_HEATER_ENTITY in changes:
            if entry.state is not ConfigEntryState.LOADED:
                return None, "replacement_not_loaded"
            try:
                await entry.runtime_data.async_replace_room_heater(
                    original[CONF_ROOM_ID], original[CONF_HEATER_ENTITY], changes
                )
            except ServiceValidationError as err:
                if err.translation_key in {"room_not_found", "room_changed"}:
                    return self.async_abort(reason=err.translation_key), ""
                return None, "heater_replacement_failed"
            return self.async_create_entry(title="", data=dict(entry.options)), ""

        options = dict(entry.options or entry.data)
        rooms = list(options.get(CONF_ROOMS, []))
        for index, room in enumerate(rooms):
            if room[CONF_ROOM_ID] == self._selected_room_id:
                if room[CONF_HEATER_ENTITY] != original[CONF_HEATER_ENTITY]:
                    return self.async_abort(reason="room_changed"), ""
                rooms[index] = {**room, **changes}
                options[CONF_ROOMS] = rooms
                return self.async_create_entry(title="", data=options), ""
        return self.async_abort(reason="room_not_found"), ""

    # Add-room mixin hooks
    def _existing_room_ids(self) -> set[str]:
        return {r[CONF_ROOM_ID] for r in self._rooms()}

    async def _async_room_added(self, room: dict[str, Any]) -> ConfigFlowResult:
        self._rooms().append(room)
        return self.async_create_entry(title="", data=self._options)
