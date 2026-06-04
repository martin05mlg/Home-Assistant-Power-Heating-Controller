# HAPHC — Home Assistant Power Heating Controller

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![HA Version](https://img.shields.io/badge/Home%20Assistant-2026.4%2B-blue)](https://www.home-assistant.io)

Smart, power-aware heating controller for Home Assistant.  
Dynamically manages electric radiators to **never exceed your contracted power limit** while maximizing thermal comfort across all rooms.

---

## Features

- **100% UI-driven setup** — no YAML required
- **Per-room priorities** — higher-priority rooms are the last to lose heating
- **Preemption** — a very cold room can displace a comfortable lower-priority one
- **Flexible hardware** — works with `climate` entities (thermostats, AC/heat pumps) or plain `switch` entities (smart plugs)
- **Reactive + polling** — responds instantly to power spikes and temperature changes
- **Runtime services** — add/remove rooms, change setpoints, update power limit without restarting HA
- **State persistence** — rooms remember their last on/off state across HA restarts
- **Clean entities** — no `input_boolean` clutter; demand/permission is internal state
- **Non-intrusive climate control** — only toggles hvac_mode on/off; never overwrites the hardware thermostat's own setpoint

---

## How It Works

### Software thermostat vs hardware thermostat

This integration implements its **own software thermostat** per room. The *Target temperature* you set in the thermostat card is used by HAPHC's algorithm to decide whether the room *wants* heat (demand). It does **not** push that temperature to the hardware thermostat.

When a room gets permission to heat, HAPHC only sets the climate entity's `hvac_mode` to `heat` (or your configured mode). The hardware thermostat's own setpoint dial/app stays in control of how hot the element runs. This is intentional — it avoids interfering with Tuya/Beok/etc. thermostats that have their own physical controls and can produce errors if HA tries to set their temperature.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                     PowerHeatingCoordinator                         │
│                                                                     │
│  power_sensor ──► available_power                                   │
│                        │                                            │
│                   ┌────▼────────────────────────────────────┐       │
│  temp_sensor ──►  │  Software Thermostat (per room)         │       │
│  target_temp ──►  │  deficit = target - current             │       │
│                   │  demand ON  when temp < target - tol    │       │
│                   │  demand OFF when temp > target + tol    │       │
│                   └────┬────────────────────────────────────┘       │
│                        │ demand signal                              │
│                   ┌────▼────────────────────────────────────┐       │
│                   │  Power Manager                          │       │
│                   │  • grant permission if watts available  │       │
│                   │  • revoke from least-urgent on overload │       │
│                   │  • preempt lower-priority for colder    │       │
│                   └────┬────────────────────────────────────┘       │
│                        │ permission signal                          │
│                   ┌────▼────────────────────────────────────┐       │
│                   │  Actuator (per room)                    │       │
│                   │  demand AND permission → hvac_mode=heat │       │
│                   │  otherwise            → hvac_mode=off   │       │
│                   └─────────────────────────────────────────┘       │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Installation

### Via HACS (recommended)
1. Open HACS → **Integrations** → three-dot menu → **Custom repositories**
2. Add `https://github.com/martin05mlg/Home-Assistant-Power-Heating-Controller` as **Integration**
3. Install **Power Heating Controller**
4. Restart Home Assistant

### Manual
1. Copy `custom_components/power_heating_controller/` to your HA `custom_components/` folder
2. Restart Home Assistant

---

## Setup

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **Power Heating Controller**
3. Follow the wizard:
   - Select your **power consumption sensor** (W)
   - Enter your **contracted power limit** (W)
   - Add rooms one by one:
     - Room name
     - Heater type: `climate` or `switch`
     - Heater entity + temperature sensor
     - Target temperature (for the software thermostat algorithm), rated power, priority

---

## Entities Created

For each room `<room_name>`:

| Entity | Type | Description |
|--------|------|-------------|
| `climate.<room_name>` | Climate | Virtual thermostat card — set target temp, enable/disable room |
| `sensor.<room_name>_heating_status` | Sensor | `heating` / `waiting` / `idle` / `disabled` |

Global:

| Entity | Type | Description |
|--------|------|-------------|
| `sensor.available_power` | Sensor | Watts left before hitting the limit |
| `sensor.current_power_consumption` | Sensor | Current total draw from the power sensor |
| `number.max_power_limit` | Number | Contracted limit (adjustable live) |

---

## Editing a Room After Setup

Go to **Settings → Devices & Services → Power Heating Controller → Configure**.

From the options menu you can edit any room's:
- **Room name**
- **Heater entity** (climate or switch)
- **Temperature sensor**
- **Target temperature**, **rated power**, **priority**
- **Enabled** toggle
- **HVAC heat mode** (advanced — for thermostats that use a non-standard mode name like `"auto"` or `"manual"` instead of `"heat"`)
- **Delete room** (also removes the orphaned entities from the registry)

---

## Services

| Service | Description |
|---------|-------------|
| `power_heating_controller.set_max_power` | Update contracted limit |
| `power_heating_controller.set_room_target_temp` | Change a room's setpoint |
| `power_heating_controller.enable_room` | Re-enable a disabled room |
| `power_heating_controller.disable_room` | Disable a room |
| `power_heating_controller.add_room` | Add a room at runtime (persisted) |
| `power_heating_controller.remove_room` | Remove a room and clean up its entities |

---

## Priority System

Priority is a number from **1** (highest) to **10** (lowest).

- When power is short, the room with the **highest priority number AND smallest thermal deficit** is preempted first (least important + most comfortable yields first).
- A high-priority room that is very cold can **displace** a lower-priority comfortable room.
- Rooms at the same priority level compete equally — the most comfortable one yields first.

**Example:** Bedroom (priority 1) vs Living room (priority 3) vs Kitchen (priority 5).  
If power runs short, the kitchen yields first. If still short, the living room. The bedroom is protected until last.

---

## FAQ

**Q: My climate entity (Tuya/Beok) shows "Unrecognised HVAC Mode" warnings.**  
A: HAPHC no longer pushes temperature setpoints to the hardware — it only toggles `hvac_mode`. If you still see the warning, check what mode name your thermostat uses. Edit the room in Options → set `HVAC heat mode` to the correct string (e.g. `"auto"` or `"manual"`).

**Q: Rooms turn on by default after a HA restart.**  
A: Fixed in v1.1.0. Rooms now persist their last on/off state using HA's RestoreEntity, so a restart leaves them as they were.

**Q: I deleted a room and its entities show "integration no longer provides this entity".**  
A: Fixed in v1.1.0. Removing a room via Options → delete or via the `remove_room` service now automatically cleans up orphaned entities from the entity registry.

**Q: Does this replace `generic_thermostat`?**  
A: Yes — the coordinator implements its own software thermostat per room.

**Q: Can I use this with underfloor heating via switches?**  
A: Yes — choose `switch` as the heater type.

**Q: I added a room via service call but it disappeared after a restart.**  
A: Fixed in v1.1.0. Rooms added via `add_room` service are now persisted to the config entry options.

---

## Changelog

### v1.1.0
- **Fix**: Rooms no longer default to ON after HA restart (RestoreEntity)
- **Fix**: Removing a room now cleans orphaned entities from entity registry
- **Fix**: No longer pushes temperature setpoint to hardware climate entities (avoids Tuya/Beok HVAC mode errors)
- **Fix**: `add_room` service now persists new rooms to config entry options
- **Fix**: `manifest.json` and `hacs.json` had wrong GitHub URL / domain
- **Improvement**: Options flow now allows editing heater entity, temp sensor and room name
- **Improvement**: Bootstrap timeout resolved (listener registration is now non-blocking)

---

## License

MIT — see [LICENSE](LICENSE)
