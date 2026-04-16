"""Constants for the Power Heating Controller integration."""

DOMAIN = "power_heating_controller"

# ── Config entry keys (global) ─────────────────────────────────────────────
CONF_POWER_SENSOR = "power_sensor"
CONF_MAX_POWER = "max_power"
CONF_ROOMS = "rooms"

# ── Per-room config keys ───────────────────────────────────────────────────
CONF_ROOM_NAME = "room_name"
CONF_ROOM_ID = "room_id"
CONF_HEATER_TYPE = "heater_type"
CONF_HEATER_ENTITY = "heater_entity"
CONF_TEMP_SENSOR = "temp_sensor"
CONF_TARGET_TEMP = "target_temp"
CONF_POWER_W = "power_w"
CONF_PRIORITY = "priority"
CONF_HVAC_MODE_HEAT = "hvac_mode_heat"
CONF_ENABLED = "enabled"

# ── Heater type values ─────────────────────────────────────────────────────
HEATER_TYPE_CLIMATE = "climate"
HEATER_TYPE_SWITCH = "switch"

# ── Internal state keys ────────────────────────────────────────────────────
STATE_DEMAND = "demand"
STATE_PERMISSION = "permission"

# ── Defaults ──────────────────────────────────────────────────────────────
DEFAULT_MAX_POWER = 5500
DEFAULT_TARGET_TEMP = 20.0
DEFAULT_POWER_W = 1000
DEFAULT_PRIORITY = 5
DEFAULT_HOT_TOLERANCE = 0.1
DEFAULT_COLD_TOLERANCE = 0.1
DEFAULT_HVAC_MODE_HEAT = "heat"

# ── Timing ────────────────────────────────────────────────────────────────
OVERPOWER_GRACE_SECONDS = 5
POWER_OK_GRACE_SECONDS = 20
COORDINATOR_UPDATE_INTERVAL = 10

# ── Platforms ─────────────────────────────────────────────────────────────
# climate  → one RoomThermostatClimate per room (the user-facing thermostat card)
# sensor   → available power + per-room status sensors
# number   → global max power limit (room target temp is now on the climate entity)
PLATFORMS = ["climate", "sensor", "number"]

# ── Services ──────────────────────────────────────────────────────────────
SERVICE_SET_MAX_POWER = "set_max_power"
SERVICE_ADD_ROOM = "add_room"
SERVICE_REMOVE_ROOM = "remove_room"
SERVICE_SET_ROOM_TARGET = "set_room_target_temp"
SERVICE_ENABLE_ROOM = "enable_room"
SERVICE_DISABLE_ROOM = "disable_room"