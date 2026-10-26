"""Feed manager: turns Frigate events into timeline items."""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .frigate_source import FrigateSource
from .models import DetectionItem
from .storage import HiddenStore


def event_to_item(event: dict[str, Any], source: FrigateSource) -> DetectionItem | None:
    """Convert one Frigate event dict into a DetectionItem.

    Returns None when the label does not map onto a feed bucket.
    - id = event["id"]; label = bucket_frigate_label(event["label"])
    - start_ts/end_ts = local ISO strings from epoch start_time/end_time
    - duration_s = rounded end - start (None while in progress)
    - camera_name = source.camera_display_name(camera)
    - source_entity_id = source.source_entity_id(camera, raw label) or ""
    - snapshot_url = MEDIA_URL(kind=snapshot.jpg) when has_snapshot, else None
    - recording:
        end_time is None              -> {"status": "pending"}
        ended and has_clip            -> {"status": "linked", "local_url": MEDIA_URL(kind=clip.mp4),
                                          "clip_start_ts", "clip_end_ts", "start_offset_s", "resolved_at"}
        ended without clip            -> {"status": "not_found"}
      start_offset_s = source.pre_capture_seconds(camera, event["data"].get("max_severity"))
    - frigate = {"event_id", "label", "sub_label", "score", "zones", "camera"}
    """
    raise NotImplementedError


class FrigateFeedManager:
    """Serve feed items from Frigate with a short cache and a persistent hidden list."""

    def __init__(
        self,
        hass: HomeAssistant,
        source: FrigateSource,
        store: HiddenStore,
        enabled_labels: set[str],
        retention_hours: int,
        max_detections: int,
    ) -> None:
        self.hass = hass
        self.source = source
        self._store = store
        self._enabled_labels = set(enabled_labels)
        self._retention_hours = retention_hours
        self._max_detections = max_detections

    async def async_start(self) -> None:
        """Load hidden ids and schedule the hourly hidden-id prune."""
        raise NotImplementedError

    async def async_stop(self) -> None:
        """Cancel timers and flush pending saves."""
        raise NotImplementedError

    def get_enabled_labels(self) -> set[str]:
        return set(self._enabled_labels)

    def get_retention_hours(self) -> int:
        return self._retention_hours

    async def async_list_items(self, labels: set[str] | None = None) -> list[DetectionItem]:
        """Return items newest-first.

        Filters: within retention, label in (labels or enabled labels), not hidden,
        at most min(max_detections, LIST_ITEMS_LIMIT). Results of the Frigate query are
        cached for LIST_CACHE_SECONDS. Raises FrigateNotReady when Frigate is unreachable.
        """
        raise NotImplementedError

    async def async_resolve_recording(self, item_id: str, *, final_attempt: bool = False) -> dict[str, Any]:
        """Re-fetch one event and return its (unsigned) recording dict.

        Raises ValueError when the event does not exist.
        """
        raise NotImplementedError

    async def async_hide_item(self, item_id: str) -> None:
        """Hide an item from the feed (never touches Frigate). Raises ValueError if unknown."""
        raise NotImplementedError

    def invalidate_cache(self) -> None:
        """Drop the cached Frigate query result."""
        raise NotImplementedError
