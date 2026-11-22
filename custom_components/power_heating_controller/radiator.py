"""Physical radiators and their links to room thermostats."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Mapping

from .const import (
    CONF_FORCE_MAX_TEMP,
    CONF_HEATER_ENTITY,
    CONF_HEATER_TYPE,
    CONF_HVAC_MODE_HEAT,
    CONF_POWER_W,
    CONF_RADIATORS,
    CONF_RADIATOR_ORDER,
    CONF_ROOM_ID,
    CONF_ROOM_IDS,
    CONF_ROOMS,
    DEFAULT_FORCE_MAX_TEMP,
    DEFAULT_HVAC_MODE_HEAT,
    DEFAULT_POWER_W,
)


@dataclass(frozen=True)
class Radiator:
    """A single switch or climate entity with one power rating."""

    heater_entity: str
    heater_type: str
    power_w: int
    hvac_mode_heat: str
    force_max_temp: bool
    room_ids: tuple[str, ...]
    primary: bool
    order: int

    @classmethod
    def from_dict(
        cls, data: Mapping[str, Any], room_ids: tuple[str, ...], *, primary: bool
    ) -> Radiator:
        return cls(
            heater_entity=data[CONF_HEATER_ENTITY],
            heater_type=data[CONF_HEATER_TYPE],
            power_w=int(data.get(CONF_POWER_W, DEFAULT_POWER_W)),
            hvac_mode_heat=data.get(CONF_HVAC_MODE_HEAT) or DEFAULT_HVAC_MODE_HEAT,
            force_max_temp=bool(data.get(CONF_FORCE_MAX_TEMP, DEFAULT_FORCE_MAX_TEMP)),
            room_ids=room_ids,
            primary=primary,
            order=0 if primary else int(data.get(CONF_RADIATOR_ORDER, 10)),
        )


@dataclass
class RadiatorState:
    """Allocation and command history for a physical radiator."""

    permission: bool = False
    granted_at: float | None = None
    last_command_at: float | None = None


def collect_radiators(options: Mapping[str, Any]) -> dict[str, Radiator]:
    """Use legacy room heaters as primaries and count duplicate devices once."""
    rooms = options.get(CONF_ROOMS, [])
    room_ids = {room[CONF_ROOM_ID] for room in rooms}
    radiators: dict[str, Radiator] = {}
    for room in rooms:
        entity = room[CONF_HEATER_ENTITY]
        room_id = room[CONF_ROOM_ID]
        if existing := radiators.get(entity):
            # Older releases allowed the same heater as the primary of two rooms.
            radiators[entity] = replace(existing, room_ids=(*existing.room_ids, room_id))
        else:
            radiators[entity] = Radiator.from_dict(room, (room_id,), primary=True)
    for radiator in options.get(CONF_RADIATORS, []):
        entity = radiator[CONF_HEATER_ENTITY]
        links = tuple(dict.fromkeys(room for room in radiator[CONF_ROOM_IDS] if room in room_ids))
        if links and entity not in radiators:
            radiators[entity] = Radiator.from_dict(radiator, links, primary=False)
    return radiators
