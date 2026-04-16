# HAPHC — Home Assistant Power Heating Controller

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://hacs.xyz)
[![HA Version](https://img.shields.io/badge/Home%20Assistant-2026.4%2B-blue)](https://www.home-assistant.io)

Smart, power-aware heating controller for Home Assistant.  
Dynamically manages electric radiators to **never exceed your contracted power limit** while maximising thermal comfort across all rooms.

---

## Features

- **100% UI-driven setup** — no YAML required
- **Per-room priorities** — higher-priority rooms are the last to lose heating
- **Preemption** — a very cold room can displace a comfortable lower-priority one
- **Flexible hardware** — works with `climate` entities (AC/heat pumps via their HA integration) or plain `switch` entities (smart plugs)
- **Reactive + polling** — responds instantly to power spikes and temperature changes
- **Runtime services** — add/remove rooms, change setpoints, update power limit without restarting HA
- **Clean entities** — no `input_boolean` clutter; demand/permission is internal state

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
│                   │  demand AND permission → heater ON      │       │
│                   │  otherwise            → heater OFF      │       │
│                   └─────────────────────────────────────────┘       │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Installation

### Via HACS (recommended)
1. Open HACS → **Integrations** → three-dot menu → **Custom repositories**
2. Add `https://github.com/yourusername/ha-power-heating-controller` as **Integration**
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
     - Target temperature, rated power, priority

---

## Entities Created

For each room `<room_id>`:

| Entity | Type | Description |
|--------|------|-------------|
| `sensor.<room_id>_heating_status` | Sensor | `heating` / `waiting` / `idle` / `disabled` |
| `switch.<room_id>_heating_enabled` | Switch | Enable/disable this room |
| `number.<room_id>_target_temperature` | Number | Adjust setpoint (5–35 °C) |

Global:

| Entity | Type | Description |
|--------|------|-------------|
| `sensor.available_power` | Sensor | Watts left before hitting limit |
| `sensor.current_power_consumption` | Sensor | Current total draw |
| `number.max_power_limit` | Number | Contracted limit (adjustable) |

---

## Services

| Service | Description |
|---------|-------------|
| `power_heating_controller.set_max_power` | Update contracted limit |
| `power_heating_controller.set_room_target_temp` | Change a room's setpoint |
| `power_heating_controller.enable_room` | Re-enable a disabled room |
| `power_heating_controller.disable_room` | Disable a room |
| `power_heating_controller.add_room` | Add a room at runtime |
| `power_heating_controller.remove_room` | Remove a room at runtime |

---

## Priority System

Priority is a number from **1** (highest) to **10** (lowest).

- When power is short, the room with the **lowest priority AND smallest thermal deficit** is preempted first.
- A high-priority room that is very cold can **displace** a lower-priority comfortable room.
- Rooms at the same priority level compete equally — the most comfortable one yields first.

**Example:** Bedroom (priority 1) vs Living room (priority 3) vs Kitchen (priority 5).  
If power runs short, the kitchen yields first. If still short, the living room. The bedroom is protected until last.

---

## FAQ

**Q: Does this replace `generic_thermostat`?**  
A: Yes — the coordinator implements its own software thermostat per room, so you don't need `generic_thermostat` entries in your config.

**Q: My climate entity uses a different mode name for heating.**  
A: After setup, edit the room in Options → the `hvac_mode_heat` field (defaults to `"heat"`).

**Q: Can I use this with underfloor heating via switches?**  
A: Yes — choose `switch` as the heater type.

---

## License

MIT — see [LICENSE](LICENSE)
