# HAPHC — Home Assistant Power Heating Controller

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![HA Version](https://img.shields.io/badge/Home%20Assistant-2025.2%2B-blue)](https://www.home-assistant.io)
[![Version](https://img.shields.io/badge/Version-2.0.0-green)](https://github.com/martin05mlg/Home-Assistant-Power-Heating-Controller)

Smart, power-aware heating controller for Home Assistant.  
Dynamically manages electric radiators and heaters to **never exceed your contracted power limit** while maximizing thermal comfort across all rooms.

---

## Features

- **100% UI-driven setup** — no YAML required (English & Spanish translations included)
- **Per-room priorities** — higher-priority rooms are the last to lose heating
- **Smart Preemption** — a colder, higher-priority room (or a much colder same-priority room) can displace a comfortable lower-priority one, respecting a minimum on-time to avoid relay chatter
- **Pending Power Reservation** — newly turned-on heaters reserve their rated wattage for 30 seconds until the household power meter reflects the new load, preventing multi-room overshoot
- **Reliable Overpower Shedding** — sustained consumption above the contracted limit sheds as many heaters as needed (least urgent first) after a 5-second grace period
- **Periodic State Reconciliation** — automatically corrects heaters left ON after restarts/reloads, missed RF/Zigbee commands, or manual external changes
- **Fail-safe Sensor Handling** — blocks new permissions if the power sensor is unavailable (supports `W` and `kW` with automatic conversion), and safely turns off heating in a room if its temperature sensor stays unavailable for more than 2 minutes
- **Flexible Hardware** — works with `climate` entities (thermostats, TRVs, AC/heat pumps) or plain `switch` entities (smart plugs, relays)
- **Optional Max-Temp Boost** — for `climate` heaters, optionally pushes the device's own `max_temp` (or 35 °C fallback) in a single atomic service call when heating is granted so the physical thermostat opens fully while HAPHC's software thermostat controls the room temperature
- **Full State Persistence** — target temperatures, room enabled/disabled states, and max power limit changes are persisted directly into the config entry options without triggering unnecessary reloads

---

## How It Works

### Software thermostat vs hardware thermostat

HAPHC implements its **own software thermostat** per room with configurable cold and hot hysteresis tolerances (default `±0.3 °C`):

1. **Demand**: When `current_temp <= target_temp - cold_tolerance`, the room requests heat (`demand = True`). When `current_temp >= target_temp + hot_tolerance`, demand turns off (`demand = False`).
2. **Permission & Power Budget**: The coordinator checks available power (`max_power - current_power - pending_power`). If enough wattage is available (or can be freed by preempting lower-priority rooms), it grants `permission = True`.
3. **Actuation**:
   - **`switch` heaters**: Calls `switch.turn_on` when granted permission, and `switch.turn_off` when revoked.
   - **`climate` heaters**: When granted permission, sets `hvac_mode` to your configured heat mode (default `heat`). If **Set the heater to its maximum temperature when heating** (`force_max_temp`) is enabled (default `True`), it sends a single `climate.set_temperature` call with both `hvac_mode` and the hardware's `max_temp` attribute so the physical radiator never throttles itself prematurely. When revoked, it sets `hvac_mode` to `off`.

---

## Architecture

```
┌────────────────────────────────────────────────────────────────────────┐
│                       PowerHeatingCoordinator                          │
│                                                                        │
│  power_sensor (W / kW) ──► available = max - current - pending_power   │
│                                 │                                      │
│                            ┌────▼───────────────────────────────────┐  │
│  temp_sensor ───────────►  │  Software Thermostat (per room)        │  │
│  target_temp ± tol ─────►  │  deficit = target - current            │  │
│                            │  demand ON  when temp <= target - cold │  │
│                            │  demand OFF when temp >= target + hot  │  │
│                            └────┬───────────────────────────────────┘  │
│                                 │ demand signal                        │
│                            ┌────▼───────────────────────────────────┐  │
│                            │  Power Manager (locked control cycle)  │  │
│                            │  • grant permission if watts available │  │
│                            │  • preempt lower-priority / warmer     │  │
│                            │  • shed load on sustained overpower    │  │
│                            │  • periodic hardware reconciliation    │  │
│                            └────┬───────────────────────────────────┘  │
│                                 │ permission signal                    │
│                            ┌────▼───────────────────────────────────┐  │
│                            │  Actuator (per room)                   │  │
│                            │  permission=True  → ON / heat (+max)   │  │
│                            │  permission=False → OFF                │  │
│                            └────────────────────────────────────────┘  │
└────────────────────────────────────────────────────────────────────────┘
```

---

## Installation

### Via HACS (recommended)
1. Open HACS → **Integrations** → three-dot menu → **Custom repositories**
2. Add `https://github.com/martin05mlg/Home-Assistant-Power-Heating-Controller` as **Integration**
3. Install **Power Heating Controller**
4. Restart Home Assistant

### Manual
1. Copy `custom_components/power_heating_controller/` to your Home Assistant `custom_components/` folder
2. Restart Home Assistant

---

## Setup

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **Power Heating Controller**
3. Follow the setup wizard:
   - **Global Setup**:
     - Select your **power consumption sensor** (`W` or `kW`)
     - Enter your **contracted power limit** (`W`)
     - Set **cold tolerance** and **hot tolerance** (`°C`, default `0.3 °C`)
   - **Add Rooms** one by one:
     - Room name & heater type (`climate` or `switch`)
     - Heater entity & room temperature sensor
     - Target temperature, rated power (`W`), priority (`1–10`)
     - For `climate` heaters: HVAC mode used to heat (default `heat`) and whether to force max temperature when heating

---

## Devices & Entities Created

HAPHC creates a main **Power Heating Controller** hub device and one **Room device** per configured room.

### Per Room `<Room Name>`

| Entity | Type | Description |
|--------|------|-------------|
| `climate.<room_id>` | Climate | Virtual thermostat — set target temperature and enable/disable (`heat` / `off`) |
| `sensor.<room_id>_heating_status` | Sensor (`enum`) | `heating` / `waiting` / `idle` / `disabled` |

### Global Hub

| Entity | Type | Description |
|--------|------|-------------|
| `sensor.power_heating_controller_available_power` | Sensor | Remaining power budget (`W`), accounting for newly started heaters |
| `sensor.power_heating_controller_current_power_consumption` | Sensor | Household consumption in `W` |
| `number.power_heating_controller_max_power_limit` | Number | Contracted power limit in `W` (persisted when changed) |

---

## Editing Global Settings or Rooms

Go to **Settings → Devices & Services → Power Heating Controller → Configure**:

- **⚡ Power limit, sensor & tolerances**: Update the power sensor, contracted power limit, or cold/hot hysteresis tolerances.
- **🏠 `<Room Name>`**: Edit the room's name, heater entity, temperature sensor, target temperature, rated power, priority, enabled state, HVAC heat mode, max-temp boost, or **delete the room** (which safely turns off its heater and cleans up its device and entities).
- **➕ Add new room**: Add another room through the UI.

---

## Services

| Service | Description |
|---------|-------------|
| `power_heating_controller.set_max_power` | Update the contracted power limit in watts (persisted) |
| `power_heating_controller.set_room_target_temp` | Change a room's target temperature (persisted) |
| `power_heating_controller.enable_room` | Enable a room (persisted) |
| `power_heating_controller.disable_room` | Disable a room and turn off its heater (persisted) |
| `power_heating_controller.add_room` | Dynamically add a new room (persisted, reloads integration) |
| `power_heating_controller.remove_room` | Turn off the heater and remove a room and its entities |

> **Tip:** You can find a room's `room_id` in the `room_id` state attribute of its `climate` entity or at the top of the **Edit room** dialog in Options.

---

## Priority & Preemption System

Priority is a number from **1** (highest importance) to **10** (lowest importance).

- **Granting order**: Waiting rooms are sorted by highest importance first (`priority` ascending), and ties are broken by largest thermal deficit (`target_temp - current_temp` descending).
- **Preemption**: If a waiting room does not have enough available power, it can preempt active rooms that have been heating for at least 2 minutes (`MIN_ON_TIME_SECONDS`) if:
  - The active room has a lower priority (higher priority number), **or**
  - Both rooms have the same priority, and the waiting room's thermal deficit is at least `1.0 °C` greater (`PREEMPT_DEFICIT_MARGIN`).
- **Overpower shedding**: When household power stays above `max_power` for 5 seconds (`OVERPOWER_GRACE_SECONDS`), active heaters are revoked starting from the lowest priority (highest number) and smallest thermal deficit until the excess load is covered. After shedding, a 20-second cooldown (`POWER_OK_GRACE_SECONDS`) prevents immediate re-granting.

---

## Changelog

### v2.0.0
- **Critical Fix**: Fixed overpower grace timer being reset on every power sensor update so load shedding reliably triggers on fast-reporting meters.
- **Critical Fix**: Added pending power reservation (`PENDING_POWER_SECONDS = 30s`) so multiple rooms aren't simultaneously granted permission from the same stale power reading.
- **Critical Fix**: Fail-safe behavior when sensors become `unavailable` (blocks new permissions when power sensor is unavailable; stops room heating if temperature sensor is unavailable for >120s).
- **Critical Fix**: Added periodic hardware state reconciliation so heaters left ON across restarts/reloads or missed commands are switched to the expected state.
- **Critical Fix**: serialized all control decisions with an `asyncio.Lock` to eliminate race conditions between state listeners, periodic polling, and overpower tasks.
- **Fix**: Wired preemption into the permission-granting cycle with minimum on-time protection (`MIN_ON_TIME_SECONDS = 120s`) and same-priority deficit margin (`1.0 °C`).
- **Fix**: Used `copy.deepcopy` in the Options Flow so editing rooms works reliably every time.
- **Fix**: Unified persistence into `entry.options` (target temperature, enabled state, max power) without triggering full reloads on runtime changes, eliminating startup flash-on bugs.
- **Feature**: Automatic `kW` → `W` unit conversion on the household power sensor.
- **Feature**: Configurable `cold_tolerance` and `hot_tolerance` in Global Settings (default `0.3 °C`).
- **Feature**: Configurable `force_max_temp` per climate room (reads the hardware entity's `max_temp` attribute and sends a single atomic `climate.set_temperature` call).
- **Feature**: Added `translations/en.json` and `translations/es.json`, per-room devices linked to the hub device, `SensorDeviceClass.ENUM` for room status, and single config entry enforcement.

### v1.1.0
- Initial public bugfix revision (entity registry cleanup, options flow editing, service registration).

---

## License

MIT — see [LICENSE](LICENSE)
