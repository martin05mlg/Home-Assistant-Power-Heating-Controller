"""Constants for the Power Heating Controller integration."""

from homeassistant.const import Platform

DOMAIN = "power_heating_controller"
VERSION = "2.2.0"
MANUFACTURER = "HAPHC"

# ── Config entry keys (global) ─────────────────────────────────────────────
CONF_POWER_SENSOR = "power_sensor"
CONF_MAX_POWER = "max_power"
CONF_ROOMS = "rooms"
CONF_RADIATORS = "radiators"
CONF_ROOM_IDS = "room_ids"
CONF_RADIATOR_ORDER = "radiator_order"
CONF_COLD_TOLERANCE = "cold_tolerance"
CONF_HOT_TOLERANCE = "hot_tolerance"

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
CONF_FORCE_MAX_TEMP = "force_max_temp"
CONF_ENABLED = "enabled"

# ── Heater type values ─────────────────────────────────────────────────────
HEATER_TYPE_CLIMATE = "climate"
HEATER_TYPE_SWITCH = "switch"
HEATER_TYPES = [HEATER_TYPE_CLIMATE, HEATER_TYPE_SWITCH]

# ── Room status values (RoomStatusSensor) ──────────────────────────────────
STATUS_HEATING = "heating"
STATUS_WAITING = "waiting"
STATUS_IDLE = "idle"
STATUS_DISABLED = "disabled"
ROOM_STATUSES = [STATUS_HEATING, STATUS_WAITING, STATUS_IDLE, STATUS_DISABLED]

WAIT_NONE = "not_waiting"
WAIT_POWER = "insufficient_power"
WAIT_POWER_SENSOR = "power_sensor_unavailable"
WAIT_TEMPERATURE_SENSOR = "temperature_sensor_unavailable"
WAIT_OVERPOWER = "power_limit_exceeded"
WAIT_COOLDOWN = "cooldown"
WAIT_ALLOCATION = "pending_allocation"
WAIT_SHUTDOWN = "awaiting_shutdown"
WAIT_RADIATOR = "radiator_unavailable"
WAIT_REASONS = [
    WAIT_NONE,
    WAIT_POWER,
    WAIT_POWER_SENSOR,
    WAIT_TEMPERATURE_SENSOR,
    WAIT_OVERPOWER,
    WAIT_COOLDOWN,
    WAIT_ALLOCATION,
    WAIT_SHUTDOWN,
    WAIT_RADIATOR,
]

# ── Defaults ──────────────────────────────────────────────────────────────
DEFAULT_MAX_POWER = 5500
DEFAULT_TARGET_TEMP = 20.0
DEFAULT_POWER_W = 1000
DEFAULT_PRIORITY = 5
DEFAULT_COLD_TOLERANCE = 0.1
DEFAULT_HOT_TOLERANCE = 0.1
DEFAULT_HVAC_MODE_HEAT = "heat"
DEFAULT_FORCE_MAX_TEMP = True
# Used when a climate heater does not expose a `max_temp` attribute.
FALLBACK_HEATER_MAX_TEMP = 35.0

# ── Limits ────────────────────────────────────────────────────────────────
MIN_MAX_POWER = 500
MAX_MAX_POWER = 50000
MIN_TARGET_TEMP = 5.0
MAX_TARGET_TEMP = 35.0
TARGET_TEMP_STEP = 0.5
MIN_PRIORITY = 1
MAX_PRIORITY = 10
MIN_ROOM_POWER = 50
MAX_ROOM_POWER = 10000

# ── Timing (seconds) ──────────────────────────────────────────────────────
COORDINATOR_UPDATE_INTERVAL = 10
# Measured consumption must stay above the limit this long before shedding load.
OVERPOWER_GRACE_SECONDS = 5
# After shedding load, no new permissions are granted for this long (anti-flapping).
POWER_OK_GRACE_SECONDS = 20
# Power of a freshly-granted heater is "reserved" for this long, until the
# power sensor has had time to reflect the new consumption.
PENDING_POWER_SECONDS = 30
# A temperature sensor unavailable for longer than this cuts that room's heating.
SENSOR_UNAVAILABLE_TIMEOUT = 120
# A room must have been heating at least this long before it can be preempted.
MIN_ON_TIME_SECONDS = 120
# Minimum time between corrective commands sent to the same heater.
RECONCILE_COOLDOWN_SECONDS = 30
# Time allowed for Home Assistant to confirm a heater has switched off before removal.
HEATER_SHUTDOWN_TIMEOUT = 5
# Same-priority preemption requires the waiting room to be this much colder (°C).
PREEMPT_DEFICIT_MARGIN = 1.0

# ── Platforms ─────────────────────────────────────────────────────────────
# climate  → one virtual thermostat per room
# sensor   → available/current power + per-room status
# number   → global max power limit
PLATFORMS = [Platform.CLIMATE, Platform.SENSOR, Platform.NUMBER]

# ── Services ──────────────────────────────────────────────────────────────
SERVICE_SET_MAX_POWER = "set_max_power"
SERVICE_ADD_ROOM = "add_room"
SERVICE_REMOVE_ROOM = "remove_room"
SERVICE_SET_ROOM_TARGET = "set_room_target_temp"
SERVICE_ENABLE_ROOM = "enable_room"
SERVICE_DISABLE_ROOM = "disable_room"
