"""Storage helpers for Frigate feed state."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import STORAGE_KEY_HIDDEN, STORAGE_VERSION


class HiddenStore:
    """Persist ids of events hidden from the feed (event_id -> event start epoch)."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY_HIDDEN)

    async def async_load(self) -> dict[str, float]:
        data = await self._store.async_load()
        if not data:
            return {}
        raw = data.get("hidden", {})
        if not isinstance(raw, dict):
            return {}
        hidden: dict[str, float] = {}
        for event_id, start_time in raw.items():
            try:
                hidden[str(event_id)] = float(start_time)
            except (TypeError, ValueError):
                continue
        return hidden

    async def async_save(self, hidden: dict[str, float]) -> None:
        await self._store.async_save({"hidden": dict(hidden)})
