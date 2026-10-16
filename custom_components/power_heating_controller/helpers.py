"""Small shared helpers for Power Heating Controller."""

from __future__ import annotations

from homeassistant.util import slugify


def make_room_id(name: str) -> str:
    """Convert a room name into a stable room_id ("Salón 1" → "salon_1")."""
    return slugify(name or "")

