"""Base entities for Power Heating Controller."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, VERSION
from .coordinator import PowerHeatingCoordinator


def hub_device_info(entry_id: str) -> DeviceInfo:
    """Device that groups the global entities."""
    return DeviceInfo(
        identifiers={(DOMAIN, entry_id)},
        name="Power Heating Controller",
        manufacturer=MANUFACTURER,
        model="Power Heating Controller",
        sw_version=VERSION,
    )


def room_device_identifier(entry_id: str, room_id: str) -> tuple[str, str]:
    """Device registry identifier of a room."""
    return (DOMAIN, f"{entry_id}_{room_id}")


class PowerHeatingEntity(CoordinatorEntity[PowerHeatingCoordinator]):
    """Global entity attached to the hub device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: PowerHeatingCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry_id = coordinator.config_entry.entry_id
        self._attr_unique_id = f"{entry_id}_{key}"
        self._attr_device_info = hub_device_info(entry_id)


class PowerHeatingRoomEntity(CoordinatorEntity[PowerHeatingCoordinator]):
    """Entity attached to a room device."""

    _attr_has_entity_name = True

    def __init__(
        self, coordinator: PowerHeatingCoordinator, room_id: str, key: str
    ) -> None:
        super().__init__(coordinator)
        self._room_id = room_id
        entry_id = coordinator.config_entry.entry_id
        cfg = coordinator.room_configs[room_id]
        self._attr_unique_id = f"{entry_id}_{room_id}_{key}"
        if "via_device_id" in DeviceInfo.__annotations__:
            hub_link = {"via_device_id": coordinator.hub_device_id}
        else:
            hub_link = {"via_device": (DOMAIN, entry_id)}
        self._attr_device_info = DeviceInfo(
            identifiers={room_device_identifier(entry_id, room_id)},
            name=cfg.room_name,
            manufacturer=MANUFACTURER,
            model="Room",
            sw_version=VERSION,
            **hub_link,
        )

    @property
    def available(self) -> bool:
        """Unavailable if the room no longer exists."""
        return super().available and self.coordinator.has_room(self._room_id)

