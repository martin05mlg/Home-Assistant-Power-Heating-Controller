"""Integration tests run inside an official Home Assistant container."""

import asyncio
from collections import Counter
from pathlib import Path
from shutil import copytree
import sys
import time
from tempfile import TemporaryDirectory
from unittest import IsolatedAsyncioTestCase, main
from unittest.mock import patch

from homeassistant import loader
from homeassistant.bootstrap import async_from_config_dict
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry, entity_registry
from homeassistant.helpers.entity import Entity

DOMAIN = "power_heating_controller"
POWER = "sensor.home_power"
TEMPERATURE = "sensor.room_temperature"
HEATER = "switch.office_heater"
REPLACEMENT = "switch.replacement"
CLIMATE_HEATER = "climate.office_heater"
CLIMATE_REPLACEMENT = "climate.replacement"
HALL = "switch.hall_radiator"
BEDROOM = "switch.bedroom_heater"
BEDROOM_TEMP = "sensor.bedroom_temperature"


class TestPowerHeatingController(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        config = self.enterContext(TemporaryDirectory())
        source = Path(__file__).resolve().parents[1] / "custom_components" / DOMAIN
        copytree(source, Path(config) / "custom_components" / DOMAIN)
        self.addCleanup(self._unmount_config, config)
        self.hass = hass = HomeAssistant(config)
        self.addAsyncCleanup(hass.async_stop)
        loader.async_setup(hass)
        config = {"climate": {}, "persistent_notification": {}}
        self.assertIs(await async_from_config_dict(config, hass), hass)
        self.commands = []
        self.shutdown = "delayed"
        self.off_reported = asyncio.Event()
        for heater in (HEATER, REPLACEMENT):
            hass.states.async_set(heater, "off")
        for heater in (CLIMATE_HEATER, CLIMATE_REPLACEMENT):
            hass.states.async_set(heater, "off", {"hvac_modes": ["off", "heat", "auto"]})
        hass.services.async_register("switch", "turn_on", self._heater_command)
        hass.services.async_register("switch", "turn_off", self._heater_command)
        hass.services.async_register("climate", "set_hvac_mode", self._heater_command)
        self._power(3000)
        self._temperature(19)

    @staticmethod
    def _unmount_config(config):
        # Python caches the custom_components namespace from each isolated instance.
        for module in tuple(sys.modules):
            if module == "custom_components" or module.startswith("custom_components."):
                del sys.modules[module]
        if config in sys.path:
            sys.path.remove(config)

    def _power(self, watts):
        self.hass.states.async_set(POWER, str(watts), {"unit_of_measurement": "W"})

    def _temperature(self, degrees):
        self.hass.states.async_set(
            TEMPERATURE, str(degrees), {"device_class": "temperature", "unit_of_measurement": "°C"}
        )

    async def _heater_command(self, call):
        entity_id = call.data["entity_id"]
        state = call.data.get("hvac_mode", "on" if call.service == "turn_on" else "off")
        self.commands.append((entity_id, state))
        attributes = self.hass.states.get(entity_id).attributes
        if state != "off":
            self.hass.states.async_set(entity_id, state, attributes)
        elif self.shutdown == "error":
            raise HomeAssistantError("Simulated shutdown failure")
        elif self.shutdown == "delayed":
            self.hass.loop.call_later(0.01, self._report_off, entity_id, attributes)

    def _report_off(self, entity_id, attributes):
        self.hass.states.async_set(entity_id, "off", attributes)
        self.off_reported.set()

    async def _setup_room(self, heater=HEATER):
        hass = self.hass
        flow = hass.config_entries.flow
        result = await flow.async_init(DOMAIN, context={"source": "user"})
        heater_type = heater.split(".")[0]
        settings = {"target_temp": 21, "power_w": 1000, "priority": 1}
        if heater_type == "climate":
            settings.update(hvac_mode_heat="heat", force_max_temp=False)
        for step, values in (
            ("user", {"power_sensor": POWER, "max_power": 4500,
                      "cold_tolerance": 0.1, "hot_tolerance": 0.1}),
            ("room_heater_type", {"room_name": "Office", "heater_type": heater_type}),
            ("room_heater_entity", {"heater_entity": heater, "temp_sensor": TEMPERATURE}),
            ("room_settings", settings),
            ("add_another", {"add_another": False}),
        ):
            self.assertEqual(result["step_id"], step)
            result = await flow.async_configure(result["flow_id"], values)
        entry = result["result"]
        await hass.async_block_till_done()
        self.assertIs(entry.state, ConfigEntryState.LOADED)
        return entry

    def _room_entity_id(self, entry, kind):
        registry = entity_registry.async_get(self.hass)
        suffix = f"_office_{kind}"
        return next(
            entity.entity_id
            for entity in entity_registry.async_entries_for_config_entry(registry, entry.entry_id)
            if entity.unique_id.endswith(suffix)
        )

    async def _edit_room(self, entry):
        options = self.hass.config_entries.options
        result = await options.async_init(entry.entry_id)
        result = await options.async_configure(result["flow_id"], {"action": "office"})
        self.assertEqual(result["step_id"], "edit_room")
        return result["flow_id"]

    async def _add_bedroom(self):
        self.hass.states.async_set(BEDROOM, "off")
        self.hass.states.async_set(BEDROOM_TEMP, "23", {"unit_of_measurement": "°C"})
        await self.hass.services.async_call(DOMAIN, "add_room", {
            "room_name": "Bedroom", "heater_type": "switch", "heater_entity": BEDROOM,
            "temp_sensor": BEDROOM_TEMP, "power_w": 1000, "priority": 1,
        }, blocking=True)
        await self.hass.async_block_till_done()

    async def _open_radiator_form(self, entry, selection="__add__"):
        options = self.hass.config_entries.options
        result = await options.async_init(entry.entry_id)
        flow_id = result["flow_id"]
        result = await options.async_configure(flow_id, {"action": "__radiators__"})
        self.assertEqual(result["step_id"], "radiators")
        result = await options.async_configure(flow_id, {"action": selection})
        self.assertEqual(result["step_id"], "radiator")
        return flow_id

    async def _add_hall(self, entry, rooms):
        self.hass.states.async_set(HALL, "off")
        flow_id = await self._open_radiator_form(entry)
        result = await self.hass.config_entries.options.async_configure(flow_id, {
            "heater_entity": HALL, "room_ids": rooms, "power_w": 600, "radiator_order": 10,
        })
        self.assertEqual(result["type"], "create_entry", result)
        await self.hass.async_block_till_done()

    @staticmethod
    def _edited_fields(heater):
        return {
            "room_name": "Office", "heater_entity": heater, "temp_sensor": TEMPERATURE,
            "enabled": True, "target_temp": 21, "power_w": 1000, "priority": 1,
        }

    async def test_delete_confirms_shutdown_before_forgetting_room(self):
        entry = await self._setup_room()
        room_id = self._room_entity_id(entry, "climate")
        options = self.hass.config_entries.options
        flow_id = await self._edit_room(entry)
        result = await options.async_configure(
            flow_id, {**self._edited_fields(HEATER), "delete_room": True}
        )
        self.assertEqual(result["step_id"], "confirm_remove_room")
        self.assertEqual(self.hass.states.get(HEATER).state, "on")

        self.shutdown = "no_confirmation"
        from custom_components.power_heating_controller import coordinator

        with patch.object(coordinator, "HEATER_SHUTDOWN_TIMEOUT", 0.05):
            result = await options.async_configure(flow_id, {})
        self.assertEqual(result["errors"]["base"], "heater_shutdown_failed")
        self.assertEqual(entry.options["rooms"][0]["heater_entity"], HEATER)
        self.assertIsNotNone(self.hass.states.get(room_id))

        self.shutdown = "delayed"
        result = await options.async_configure(flow_id, {})
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(self.hass.states.get(HEATER).state, "off")
        await self.hass.async_block_till_done()
        self.assertIsNone(self.hass.states.get(room_id))
        registry = device_registry.async_get(self.hass)
        self.assertTrue(all(
            device.name != "Office"
            for device in device_registry.async_entries_for_config_entry(registry, entry.entry_id)
        ))

    async def test_remove_service_surfaces_shutdown_error(self):
        entry = await self._setup_room(CLIMATE_HEATER)
        self.shutdown = "error"
        with self.assertRaises(ServiceValidationError) as raised:
            await self.hass.services.async_call(DOMAIN, "remove_room", {"room_id": "office"}, blocking=True)
        self.assertEqual(raised.exception.translation_key, "heater_shutdown_failed")
        self.assertEqual(entry.options["rooms"][0]["heater_entity"], CLIMATE_HEATER)

        self.shutdown = "delayed"
        await self.hass.services.async_call(DOMAIN, "remove_room", {"room_id": "office"}, blocking=True)
        self.assertEqual(self.hass.states.get(CLIMATE_HEATER).state, "off")
        self.assertEqual(entry.options["rooms"], [])

    async def test_replace_switch_requires_shutdown_and_keeps_entity(self):
        entry = await self._setup_room()
        room_id = self._room_entity_id(entry, "climate")
        options = self.hass.config_entries.options
        flow_id = await self._edit_room(entry)
        self.shutdown = "error"
        result = await options.async_configure(flow_id, self._edited_fields(REPLACEMENT))
        self.assertEqual(result["errors"]["base"], "heater_replacement_failed")
        self.assertEqual(entry.options["rooms"][0]["heater_entity"], HEATER)
        self.assertEqual(self.hass.states.get(REPLACEMENT).state, "off")

        self.shutdown = "delayed"
        result = await options.async_configure(flow_id, self._edited_fields(REPLACEMENT))
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(self.hass.states.get(HEATER).state, "off")
        await self.hass.async_block_till_done()
        self.assertEqual(self.hass.states.get(REPLACEMENT).state, "on")
        self.assertEqual(self._room_entity_id(entry, "climate"), room_id)
        self.assertEqual(self.commands[-2:], [(HEATER, "off"), (REPLACEMENT, "on")])

    async def test_replace_climate_only_switches_after_mode_selection(self):
        entry = await self._setup_room(CLIMATE_HEATER)
        options = self.hass.config_entries.options
        flow_id = await self._edit_room(entry)
        result = await options.async_configure(
            flow_id, {**self._edited_fields(CLIMATE_REPLACEMENT),
                      "hvac_mode_heat": "heat", "force_max_temp": False}
        )
        self.assertEqual(result["step_id"], "edit_room_hvac")
        self.assertEqual(self.hass.states.get(CLIMATE_HEATER).state, "heat")
        result = await options.async_configure(flow_id, {"hvac_mode_heat": "auto"})
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(self.hass.states.get(CLIMATE_HEATER).state, "off")
        await self.hass.async_block_till_done()
        self.assertEqual(entry.options["rooms"][0]["hvac_mode_heat"], "auto")
        self.assertEqual(self.commands[-2:], [(CLIMATE_HEATER, "off"), (CLIMATE_REPLACEMENT, "auto")])

    async def test_power_burst_writes_only_changed_entities(self):
        self._temperature(22)
        entry = await self._setup_room()
        for room_index in range(3):
            heater = f"switch.extra_{room_index}"
            self.hass.states.async_set(heater, "off")
            await self.hass.services.async_call(DOMAIN, "add_room", {
                "room_name": f"Extra {room_index}", "heater_type": "switch",
                "heater_entity": heater, "temp_sensor": TEMPERATURE,
            }, blocking=True)
            await self.hass.async_block_till_done()

        registry = entity_registry.async_get(self.hass)
        related = {
            entity.entity_id: entity.unique_id
            for entity in entity_registry.async_entries_for_config_entry(registry, entry.entry_id)
        }
        writes = Counter()
        write_state = Entity.async_write_ha_state

        def count_write(entity):
            if entity.entity_id in related:
                writes[related[entity.entity_id]] += 1
            return write_state(entity)

        with patch.object(Entity, "async_write_ha_state", count_write):
            for reading in range(120):
                self._power(500 + reading)
                await asyncio.sleep(0)
            await self.hass.async_block_till_done()

        total = sum(writes.values())
        print(f"Power burst: 120 readings, {len(related)} entities, {total} state writes", flush=True)
        self.assertEqual(total, 240, writes)
        self.assertTrue(all(key.endswith(("_available_power", "_current_power")) for key in writes))
        self.assertEqual(entry.runtime_data.data["available_power"], 3881)

    async def test_overpower_still_schedules_shedding_on_first_reading(self):
        entry = await self._setup_room()
        from custom_components.power_heating_controller import coordinator

        reason_id = self._room_entity_id(entry, "waiting_reason")
        with patch.object(coordinator, "OVERPOWER_GRACE_SECONDS", 0.03):
            self._power(4900)
            await asyncio.sleep(0)
            task = entry.runtime_data._overpower_task
            self.assertIsNotNone(task)
            await asyncio.wait_for(task, timeout=1)
        await asyncio.wait_for(self.off_reported.wait(), timeout=1)
        await self.hass.async_block_till_done()
        self.assertEqual(self.hass.states.get(HEATER).state, "off")
        self.assertEqual(self.hass.states.get(reason_id).state, "power_limit_exceeded")
        self._power(3000)
        await self.hass.async_block_till_done()
        self.assertEqual(self.hass.states.get(reason_id).state, "cooldown")

    async def test_shared_radiator_is_counted_once_and_releases_power_safely(self):
        self._power(2000)
        entry = await self._setup_room()
        await self._add_bedroom()
        await self._add_hall(entry, ["office", "bedroom"])
        coordinator = entry.runtime_data
        self.assertEqual(self.hass.states.get(HALL).state, "on")
        self.assertEqual(self.commands.count((HALL, "on")), 1)
        self.assertEqual(coordinator._pending_power(time.monotonic()), 1600)
        snapshot = coordinator.data["rooms"]
        shared_office = next(r for r in snapshot["office"]["radiators"] if r["entity_id"] == HALL)
        shared_bedroom = next(r for r in snapshot["bedroom"]["radiators"] if r["entity_id"] == HALL)
        self.assertEqual(shared_office, shared_bedroom)
        self.assertEqual(shared_office["role"], "shared")
        self.assertFalse(coordinator.room_has_permission("bedroom"))
        for radiator in coordinator.radiator_states.values():
            if radiator.granted_at is not None:
                radiator.granted_at -= 121

        self.shutdown = "no_confirmation"
        self._power(3600)
        self.hass.states.async_set(BEDROOM_TEMP, "16")
        await coordinator.async_refresh()
        await self.hass.async_block_till_done()
        self.assertEqual(self.hass.states.get(HALL).state, "on")
        self.assertEqual(self.hass.states.get(BEDROOM).state, "off")
        self.assertEqual(coordinator.data["rooms"]["bedroom"]["waiting_reason"], "awaiting_shutdown")
        self.assertEqual(self.hass.states.get(HEATER).state, "on")

        # A meter update while the hall is still on is insufficient.
        self._power(3000)
        await coordinator.async_refresh()
        self.assertEqual(self.hass.states.get(BEDROOM).state, "off")
        self.hass.states.async_set(HALL, "off")
        await coordinator.async_refresh()
        self.assertEqual(self.hass.states.get(BEDROOM).state, "off")

        self._power(3010)
        await coordinator.async_refresh()
        await self.hass.async_block_till_done()
        self.assertEqual(self.hass.states.get(BEDROOM).state, "on")
        self.assertEqual(self.commands[-2:], [(HALL, "off"), (BEDROOM, "on")])
        self.assertEqual(self.hass.states.get(HEATER).state, "on")

    async def test_room_removal_keeps_shared_radiator_and_retires_dedicated_support(self):
        self._power(1700)
        entry = await self._setup_room()
        await self._add_bedroom()
        await self._add_hall(entry, ["office", "bedroom"])
        dedicated = "switch.office_support"
        self.hass.states.async_set(dedicated, "off")
        flow_id = await self._open_radiator_form(entry)
        result = await self.hass.config_entries.options.async_configure(flow_id, {
            "heater_entity": dedicated, "room_ids": ["office"], "power_w": 450,
            "radiator_order": 20,
        })
        self.assertEqual(result["type"], "create_entry", result)
        await self.hass.async_block_till_done()
        self.assertEqual(self.hass.states.get(dedicated).state, "on")
        self.assertEqual(self.hass.states.get(HALL).state, "on")
        await self.hass.services.async_call(DOMAIN, "remove_room", {"room_id": "bedroom"}, blocking=True)
        await self.hass.async_block_till_done()
        self.assertEqual(entry.options["radiators"][0]["room_ids"], ["office"])
        self.assertNotIn((HALL, "off"), self.commands)
        await self.hass.services.async_call(DOMAIN, "remove_room", {"room_id": "office"}, blocking=True)
        await self.hass.async_block_till_done()
        self.assertEqual(entry.options["radiators"], [])
        self.assertEqual(self.hass.states.get(dedicated).state, "off")
        self.assertEqual(self.hass.states.get(HALL).state, "off")

    async def test_climate_support_uses_mode_picker_and_rejects_duplicate_entity(self):
        self._power(1500)
        entry = await self._setup_room()
        flow_id = await self._open_radiator_form(entry)
        options = self.hass.config_entries.options
        fields = {"room_ids": ["office"], "power_w": 600, "radiator_order": 10}
        result = await options.async_configure(flow_id, {**fields, "heater_entity": HEATER})
        self.assertEqual(result["errors"]["heater_entity"], "heater_already_configured")
        result = await options.async_configure(flow_id, {**fields, "heater_entity": CLIMATE_HEATER})
        self.assertEqual(result["step_id"], "radiator_hvac")
        result = await options.async_configure(flow_id, {"hvac_mode_heat": "auto", "force_max_temp": False})
        self.assertEqual(result["type"], "create_entry")
        await self.hass.async_block_till_done()
        self.assertEqual(self.hass.states.get(CLIMATE_HEATER).state, "auto")

        flow_id = await self._open_radiator_form(entry, CLIMATE_HEATER)
        result = await options.async_configure(flow_id, {
            **fields, "heater_entity": CLIMATE_HEATER, "delete_radiator": True,
        })
        self.assertEqual(result["step_id"], "confirm_remove_radiator")
        self.shutdown = "error"
        result = await options.async_configure(flow_id, {})
        self.assertEqual(result["errors"]["base"], "heater_shutdown_failed")
        self.assertEqual(self.hass.states.get(CLIMATE_HEATER).state, "auto")
        self.assertEqual(len(entry.options["radiators"]), 1)
        self.shutdown = "delayed"
        result = await options.async_configure(flow_id, {})
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(self.hass.states.get(CLIMATE_HEATER).state, "off")
        self.assertEqual(entry.options["radiators"], [])

    async def test_primary_preemption_also_waits_for_confirmed_shutdown(self):
        entry = await self._setup_room()
        await self._add_bedroom()
        coordinator = entry.runtime_data
        coordinator.radiator_states[HEATER].granted_at -= 121
        self.shutdown = "no_confirmation"
        self._power(3900)
        self.hass.states.async_set(BEDROOM_TEMP, "16")
        await coordinator.async_refresh()
        await self.hass.async_block_till_done()
        self.assertIn((HEATER, "off"), self.commands)
        self.assertEqual(self.hass.states.get(HEATER).state, "on")
        self.assertEqual(self.hass.states.get(BEDROOM).state, "off")

    async def test_sensor_readings_validate_units_and_finite_values(self):
        self.hass.states.async_set(POWER, "0.5", {"unit_of_measurement": "kW"})
        self.hass.states.async_set(TEMPERATURE, "68", {"unit_of_measurement": "°F"})
        entry = await self._setup_room()
        coordinator = entry.runtime_data
        self.assertEqual(coordinator.get_current_power(), 500)
        self.assertAlmostEqual(coordinator.data["rooms"]["office"]["current_temp"], 20)
        for reading, unit in [("nan", "W"), ("-inf", "W"), ("250", "kWh")]:
            self.hass.states.async_set(POWER, reading, {"unit_of_measurement": unit})
            self.assertIsNone(coordinator.get_current_power())
        for reading, unit in [("nan", "°C"), ("-inf", "°C"), ("250", "W")]:
            self.hass.states.async_set(TEMPERATURE, reading, {"unit_of_measurement": unit})
            await coordinator.async_refresh()
            self.assertIsNone(coordinator.data["rooms"]["office"]["current_temp"])


if __name__ == "__main__":
    main()
