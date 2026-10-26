"""Feed manager: turns Frigate events into timeline items."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import logging
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    CLEANUP_INTERVAL_SECONDS,
    LIST_CACHE_SECONDS,
    LIST_ITEMS_LIMIT,
    MEDIA_KIND_CLIP,
    MEDIA_KIND_SNAPSHOT,
    MEDIA_URL,
    bucket_frigate_label,
    frigate_labels_for_buckets,
)
from .frigate_source import FrigateSource
from .models import DetectionItem
from .storage import HiddenStore

_LOGGER = logging.getLogger(__name__)

# Extra events requested from Frigate on top of the item cap, so hidden or unmapped
# events do not push the visible list below max_detections.
_QUERY_HEADROOM = 50


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _local_iso(epoch: float) -> str:
    return dt_util.as_local(dt_util.utc_from_timestamp(epoch)).isoformat()


def _event_data(event: dict[str, Any]) -> dict[str, Any]:
    data = event.get("data")
    return data if isinstance(data, dict) else {}


def _event_score(event: dict[str, Any]) -> float | None:
    data = _event_data(event)
    for value in (data.get("top_score"), event.get("top_score"), data.get("score")):
        score = _as_float(value)
        if score is not None:
            return score
    return None


def _event_recording(
    event: dict[str, Any], source: FrigateSource, start: float, end: float | None
) -> dict[str, Any]:
    if end is None:
        return {"status": "pending"}
    if not event.get("has_clip"):
        return {"status": "not_found"}
    camera = str(event.get("camera") or "")
    severity = _event_data(event).get("max_severity")
    pre_capture = float(source.pre_capture_seconds(camera, severity))
    post_capture = float(source.post_capture_seconds(camera, severity))
    return {
        "status": "linked",
        "local_url": MEDIA_URL.format(event_id=event["id"], kind=MEDIA_KIND_CLIP),
        "clip_start_ts": _local_iso(start - pre_capture),
        "clip_end_ts": _local_iso(end + post_capture),
        "start_offset_s": pre_capture,
        "resolved_at": dt_util.now().isoformat(),
    }


def _resolve_event_recording(event: dict[str, Any], source: FrigateSource) -> dict[str, Any] | None:
    """Return the recording dict for an event regardless of its label (None if malformed)."""
    start = _as_float(event.get("start_time"))
    if not event.get("id") or start is None:
        return None
    return _event_recording(event, source, start, _as_float(event.get("end_time")))


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
    event_id = str(event.get("id") or "")
    raw_label = str(event.get("label") or "")
    label = bucket_frigate_label(raw_label)
    start = _as_float(event.get("start_time"))
    if not event_id or label is None or start is None:
        return None
    end = _as_float(event.get("end_time"))
    camera = str(event.get("camera") or "")
    zones = event.get("zones")
    return DetectionItem(
        id=event_id,
        start_ts=_local_iso(start),
        end_ts=_local_iso(end) if end is not None else None,
        duration_s=round(max(end - start, 0.0)) if end is not None else None,
        label=label,
        source_entity_id=source.source_entity_id(camera, raw_label) or "",
        camera_name=source.camera_display_name(camera),
        snapshot_url=(
            MEDIA_URL.format(event_id=event_id, kind=MEDIA_KIND_SNAPSHOT)
            if event.get("has_snapshot")
            else None
        ),
        recording=_event_recording(event, source, start, end),
        frigate={
            "event_id": event_id,
            "label": raw_label,
            "sub_label": event.get("sub_label"),
            "score": _event_score(event),
            "zones": list(zones) if isinstance(zones, (list, tuple)) else [],
            "camera": camera,
        },
    )


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
        self._hidden: dict[str, float] = {}
        # (monotonic fetched_at, query limit, raw events newest-first)
        self._cache: tuple[float, int, list[dict[str, Any]]] | None = None
        self._unsub_cleanup: Callable[[], None] | None = None

    async def async_start(self) -> None:
        """Load hidden ids and schedule the hourly hidden-id prune."""
        self._hidden = await self._store.async_load()
        await self._async_prune_hidden()
        self._unsub_cleanup = async_track_time_interval(
            self.hass, self._async_handle_cleanup, timedelta(seconds=CLEANUP_INTERVAL_SECONDS)
        )

    async def async_stop(self) -> None:
        """Cancel timers and flush pending saves."""
        # Hidden ids are saved immediately on change, so there is nothing to flush.
        if self._unsub_cleanup is not None:
            self._unsub_cleanup()
            self._unsub_cleanup = None
        self.invalidate_cache()

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
        wanted = self._enabled_labels if labels is None else self._enabled_labels & set(labels)
        if not wanted:
            return []
        cap = min(self._max_detections, LIST_ITEMS_LIMIT)
        cutoff = self._retention_cutoff()
        items: list[DetectionItem] = []
        for event in await self._async_query_events(cap):
            event_id = str(event.get("id") or "")
            if not event_id or event_id in self._hidden:
                continue
            start = _as_float(event.get("start_time"))
            if start is None or start < cutoff:
                continue
            item = event_to_item(event, self.source)
            if item is None or item.label not in wanted:
                continue
            items.append(item)
        items.sort(key=lambda item: item.start_dt, reverse=True)
        return items[:cap]

    async def async_resolve_recording(self, item_id: str, *, final_attempt: bool = False) -> dict[str, Any]:
        """Re-fetch one event and return its (unsigned) recording dict.

        Raises ValueError when the event does not exist.
        """
        event = await self.source.async_get_event(item_id)
        if event is None:
            raise ValueError(f"Unknown item id: {item_id}")
        recording = _resolve_event_recording(event, self.source)
        if recording is None:
            raise ValueError(f"Malformed Frigate event: {item_id}")
        return recording

    async def async_hide_item(self, item_id: str) -> None:
        """Hide an item from the feed (never touches Frigate). Raises ValueError if unknown."""
        event = self._cached_event(item_id)
        if event is None:
            event = await self.source.async_get_event(item_id)
        if event is None:
            raise ValueError(f"Unknown item id: {item_id}")
        start = _as_float(event.get("start_time"))
        self._hidden[item_id] = start if start is not None else time.time()
        await self._store.async_save(self._hidden)
        self.invalidate_cache()

    def invalidate_cache(self) -> None:
        """Drop the cached Frigate query result."""
        self._cache = None

    def _retention_cutoff(self) -> float:
        return time.time() - self._retention_hours * 3600

    async def _async_query_events(self, cap: int) -> list[dict[str, Any]]:
        """Query Frigate for all enabled labels, reusing a recent result."""
        limit = cap + len(self._hidden) + _QUERY_HEADROOM
        now = time.monotonic()
        if self._cache is not None:
            fetched_at, cached_limit, events = self._cache
            if now - fetched_at < LIST_CACHE_SECONDS and cached_limit >= limit:
                return events
        events = await self.source.async_get_events(
            after=self._retention_cutoff(),
            limit=limit,
            labels=frigate_labels_for_buckets(self._enabled_labels),
        )
        self._cache = (now, limit, events)
        return events

    def _cached_event(self, item_id: str) -> dict[str, Any] | None:
        if self._cache is None:
            return None
        for event in self._cache[2]:
            if event.get("id") == item_id:
                return event
        return None

    async def _async_prune_hidden(self) -> None:
        cutoff = self._retention_cutoff()
        expired = [event_id for event_id, start in self._hidden.items() if start < cutoff]
        if not expired:
            return
        for event_id in expired:
            self._hidden.pop(event_id, None)
        await self._store.async_save(self._hidden)
        _LOGGER.debug("Pruned %s expired hidden ids", len(expired))

    async def _async_handle_cleanup(self, _now: datetime) -> None:
        await self._async_prune_hidden()
