"""Feed manager: turns Frigate review items into timeline items."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import logging
import math
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    CLEANUP_INTERVAL_SECONDS,
    CLIP_READY_GRACE_SECONDS,
    EVENTS_QUERY_LIMIT,
    LABEL_PRIORITY,
    LIST_CACHE_SECONDS,
    LIST_ITEMS_LIMIT,
    MEDIA_KIND_CLIP,
    MEDIA_KIND_SNAPSHOT,
    MEDIA_URL,
    MEDIA_VERSION_PARAM,
    REVIEW_MEDIA_KIND_THUMB,
    REVIEW_MEDIA_URL,
    THUMBNAIL_QUERY,
    bucket_frigate_label,
    frigate_labels_for_buckets,
)
from .frigate_source import FrigateSource
from .models import DetectionItem
from .storage import HiddenStore

_LOGGER = logging.getLogger(__name__)

# Extra review items requested from Frigate on top of the item cap, so hidden or
# unmapped ones do not push the visible list below max_detections.
_QUERY_HEADROOM = 50
_EVENT_LOOKBACK_SECONDS = 600


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _local_iso(epoch: float) -> str:
    return dt_util.as_local(dt_util.utc_from_timestamp(epoch)).isoformat()


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _event_score(event: dict[str, Any]) -> float | None:
    data = _as_dict(event.get("data"))
    for value in (data.get("top_score"), event.get("top_score"), data.get("score")):
        score = _as_float(value)
        if score is not None:
            return score
    return None


def _review_camera(review: dict[str, Any]) -> str:
    return str(review.get("camera") or "")


def _review_detection_ids(review: dict[str, Any]) -> list[str]:
    detections = _as_dict(review.get("data")).get("detections")
    return [str(event_id) for event_id in detections] if isinstance(detections, list) else []


def _ordered_labels(labels: set[str]) -> list[str]:
    return [label for label in LABEL_PRIORITY if label in labels]


def clip_bounds(review: dict[str, Any], source: FrigateSource, start: float, end: float) -> tuple[int, int]:
    """Whole-second [start, end] of the recording clip for a review item.

    The review window padded with the camera's pre/post capture for its severity,
    which is what Frigate itself records around an alert or detection.
    """
    camera = _review_camera(review)
    severity = review.get("severity")
    pre_capture = float(source.pre_capture_seconds(camera, severity))
    post_capture = float(source.post_capture_seconds(camera, severity))
    return math.floor(start - pre_capture), math.ceil(end + post_capture)


def clip_ready_at(review: dict[str, Any], source: FrigateSource, end: float) -> float:
    """Epoch time from which Frigate has written the recording segments of an ended review."""
    post_capture = float(source.post_capture_seconds(_review_camera(review), review.get("severity")))
    return end + post_capture + CLIP_READY_GRACE_SECONDS


def _review_recording(
    review: dict[str, Any], source: FrigateSource, start: float, end: float | None
) -> dict[str, Any]:
    if end is None:
        return {"status": "pending"}
    if not source.recording_enabled(_review_camera(review)):
        return {"status": "not_found"}
    if time.time() < clip_ready_at(review, source, end):
        return {"status": "pending"}
    clip_start, clip_end = clip_bounds(review, source, start, end)
    return {
        "status": "linked",
        "local_url": REVIEW_MEDIA_URL.format(review_id=review["id"], kind=MEDIA_KIND_CLIP),
        "clip_start_ts": _local_iso(clip_start),
        "clip_end_ts": _local_iso(clip_end),
        "start_offset_s": round(start - clip_start, 3),
        "resolved_at": dt_util.now().isoformat(),
    }


def _resolve_review_recording(review: dict[str, Any], source: FrigateSource) -> dict[str, Any] | None:
    """Return the recording dict for a review item (None if malformed)."""
    start = _as_float(review.get("start_time"))
    if not review.get("id") or start is None:
        return None
    return _review_recording(review, source, start, _as_float(review.get("end_time")))


def _detection(event: dict[str, Any], label: str, clip_start: float) -> dict[str, Any]:
    start = float(event["start_time"])
    end = _as_float(event.get("end_time"))
    return {
        "event_id": str(event["id"]),
        "label": label,
        "frigate_label": str(event.get("label") or ""),
        "sub_label": event.get("sub_label"),
        "score": _event_score(event),
        "start_offset_s": round(max(start - clip_start, 0.0), 3),
        "duration_s": round(max(end - start, 0.0)) if end is not None else None,
        "has_snapshot": bool(event.get("has_snapshot")),
    }


def _snapshot_box(event: dict[str, Any] | None) -> list[float] | None:
    """The object box on the event snapshot as [x, y, width, height] fractions of the frame."""
    box = _as_dict(event.get("data")).get("box") if event else None
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return None
    values = [_as_float(value) for value in box]
    if any(value is None or not 0.0 <= value <= 1.0 for value in values):
        return None
    return [round(value, 4) for value in values]  # type: ignore[arg-type]


def _best_event(events: list[dict[str, Any]], label: str) -> dict[str, Any] | None:
    """The detection whose snapshot represents the item: main label first, then score."""
    candidates = [event for event in events if event.get("has_snapshot")]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda event: (bucket_frigate_label(event.get("label")) == label, _event_score(event) or 0.0),
    )


def _versioned_media_url(path: str, end: float | None) -> str:
    """Mark the media of an ended review item as final (MEDIA_VERSION_PARAM = its end time)."""
    if end is None:
        return path
    separator = "&" if "?" in path else "?"
    return f"{path}{separator}{MEDIA_VERSION_PARAM}={int(end)}"


def review_to_item(
    review: dict[str, Any],
    source: FrigateSource,
    events: dict[str, dict[str, Any]],
    wanted: set[str] | None = None,
) -> DetectionItem | None:
    """Convert one Frigate review item (plus its known events) into a DetectionItem.

    - id = review["id"]; labels = feed buckets of the review objects and its events,
      limited to `wanted`, in LABEL_PRIORITY order; label = labels[0]
      (None when no label maps onto a wanted bucket)
    - start_ts/end_ts/duration_s from the review window (end None while in progress)
    - detections = the review's events found in `events` with a wanted label, by start,
      with offsets into the recording clip
    - snapshot_url = snapshot of the best detection, else the review thumbnail
    - thumbnail_url = the best detection's snapshot cropped to the object (None without one)
    - recording = the camera recording over the review window padded with pre/post
      capture, served by the review media proxy:
        in progress                    -> {"status": "pending"}
        recording disabled for camera  -> {"status": "not_found"}
        ended, before clip_ready_at    -> {"status": "pending"}
        ended                          -> {"status": "linked", "local_url", "clip_start_ts",
                                           "clip_end_ts", "start_offset_s", "resolved_at"}
    """
    review_id = str(review.get("id") or "")
    start = _as_float(review.get("start_time"))
    if not review_id or start is None:
        return None
    data = _as_dict(review.get("data"))
    objects = data.get("objects") if isinstance(data.get("objects"), list) else []
    review_events = [
        events[event_id]
        for event_id in _review_detection_ids(review)
        if event_id in events and _as_float(events[event_id].get("start_time")) is not None
    ]
    buckets = {bucket_frigate_label(obj) for obj in objects}
    buckets |= {bucket_frigate_label(event.get("label")) for event in review_events}
    buckets.discard(None)
    if wanted is not None:
        buckets &= wanted
    labels = _ordered_labels(buckets)
    if not labels:
        return None
    label = labels[0]

    end = _as_float(review.get("end_time"))
    camera = _review_camera(review)
    recording = _review_recording(review, source, start, end)
    clip_start = clip_bounds(review, source, start, end if end is not None else start)[0]
    detections = sorted(
        (
            _detection(event, bucket, clip_start)
            for event in review_events
            if (bucket := bucket_frigate_label(event.get("label"))) in buckets
        ),
        key=lambda detection: detection["start_offset_s"],
    )
    best = _best_event([event for event in review_events if bucket_frigate_label(event.get("label")) in buckets], label)
    raw_label = str(best.get("label")) if best else next(
        (str(obj) for obj in objects if bucket_frigate_label(obj) == label), label
    )
    zones = data.get("zones")
    sub_labels = data.get("sub_labels")
    return DetectionItem(
        id=review_id,
        start_ts=_local_iso(start),
        end_ts=_local_iso(end) if end is not None else None,
        duration_s=round(max(end - start, 0.0)) if end is not None else None,
        label=label,
        labels=labels,
        source_entity_id=source.source_entity_id(camera, raw_label.removesuffix("-verified")) or "",
        camera_name=source.camera_display_name(camera),
        snapshot_url=_versioned_media_url(
            MEDIA_URL.format(event_id=best["id"], kind=MEDIA_KIND_SNAPSHOT)
            if best
            else REVIEW_MEDIA_URL.format(review_id=review_id, kind=REVIEW_MEDIA_KIND_THUMB),
            end,
        ),
        thumbnail_url=(
            _versioned_media_url(
                f"{MEDIA_URL.format(event_id=best['id'], kind=MEDIA_KIND_SNAPSHOT)}?{THUMBNAIL_QUERY}", end
            )
            if best
            else None
        ),
        recording=recording,
        detections=detections,
        frigate={
            "review_id": review_id,
            "event_id": str(best["id"]) if best else None,
            "severity": review.get("severity"),
            "camera": camera,
            "label": raw_label,
            "sub_label": best.get("sub_label") if best else None,
            "score": _event_score(best) if best else None,
            "box": _snapshot_box(best),
            "objects": [str(obj) for obj in objects],
            "sub_labels": list(sub_labels) if isinstance(sub_labels, list) else [],
            "zones": list(zones) if isinstance(zones, list) else [],
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
        # (monotonic fetched_at, query limit, raw review items newest-first, events by id)
        self._cache: tuple[float, int, list[dict[str, Any]], dict[str, dict[str, Any]]] | None = None
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
        """Return items (one per Frigate review item) newest-first.

        Filters: within retention, a label in (labels or enabled labels), not hidden
        (by review id, or by the id of one of its detections as hidden before review
        items were used), at most min(max_detections, LIST_ITEMS_LIMIT). Frigate query
        results are cached for LIST_CACHE_SECONDS. Raises FrigateNotReady when Frigate
        is unreachable.
        """
        wanted = self._enabled_labels if labels is None else self._enabled_labels & set(labels)
        if not wanted:
            return []
        cap = min(self._max_detections, LIST_ITEMS_LIMIT)
        cutoff = self._retention_cutoff()
        reviews, events = await self._async_query(cap)
        items: list[DetectionItem] = []
        for review in reviews:
            if self._is_hidden(review):
                continue
            start = _as_float(review.get("start_time"))
            if start is None or start < cutoff:
                continue
            item = review_to_item(review, self.source, events, wanted)
            if item is None:
                continue
            items.append(item)
        items.sort(key=lambda item: item.start_dt, reverse=True)
        return items[:cap]

    async def async_resolve_recording(self, item_id: str, *, final_attempt: bool = False) -> dict[str, Any]:
        """Re-fetch one review item and return its (unsigned) recording dict.

        Raises ValueError when the review item does not exist.
        """
        review = await self.source.async_get_review(item_id)
        if review is None:
            raise ValueError(f"Unknown item id: {item_id}")
        recording = _resolve_review_recording(review, self.source)
        if recording is None:
            raise ValueError(f"Malformed Frigate review item: {item_id}")
        return recording

    async def async_get_review(self, review_id: str) -> dict[str, Any] | None:
        """Return a review item from the list cache, else from Frigate (None if unknown)."""
        return self._cached_review(review_id) or await self.source.async_get_review(review_id)

    async def async_hide_item(self, item_id: str) -> None:
        """Hide an item from the feed (never touches Frigate). Raises ValueError if unknown."""
        review = await self.async_get_review(item_id)
        if review is None:
            raise ValueError(f"Unknown item id: {item_id}")
        start = _as_float(review.get("start_time"))
        self._hidden[item_id] = start if start is not None else time.time()
        await self._store.async_save(self._hidden)
        self.invalidate_cache()

    def invalidate_cache(self) -> None:
        """Drop the cached Frigate query result."""
        self._cache = None

    def _retention_cutoff(self) -> float:
        return time.time() - self._retention_hours * 3600

    def _is_hidden(self, review: dict[str, Any]) -> bool:
        review_id = str(review.get("id") or "")
        if not review_id or review_id in self._hidden:
            return True
        return any(event_id in self._hidden for event_id in _review_detection_ids(review))

    async def _async_query(self, cap: int) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
        """Query Frigate review items and their events for enabled labels, reusing a recent result."""
        limit = cap + len(self._hidden) + _QUERY_HEADROOM
        now = time.monotonic()
        if self._cache is not None:
            fetched_at, cached_limit, reviews, events = self._cache
            if now - fetched_at < LIST_CACHE_SECONDS and cached_limit >= limit:
                return reviews, events
        cutoff = self._retention_cutoff()
        frigate_labels = frigate_labels_for_buckets(self._enabled_labels)
        reviews = await self.source.async_get_reviews(after=cutoff, limit=limit, labels=frigate_labels)
        events: dict[str, dict[str, Any]] = {}
        if reviews:
            # An event can start before its review item does (it becomes an alert or
            # detection later, e.g. on entering a zone), so look back a little further.
            oldest = min(_as_float(review.get("start_time")) or cutoff for review in reviews)
            for event in await self.source.async_get_events(
                after=max(oldest, cutoff) - _EVENT_LOOKBACK_SECONDS,
                limit=EVENTS_QUERY_LIMIT,
                labels=frigate_labels,
            ):
                if event.get("id"):
                    events[str(event["id"])] = event
        self._cache = (now, limit, reviews, events)
        return reviews, events

    def _cached_review(self, review_id: str) -> dict[str, Any] | None:
        if self._cache is None:
            return None
        for review in self._cache[2]:
            if review.get("id") == review_id:
                return review
        return None

    async def _async_prune_hidden(self) -> None:
        cutoff = self._retention_cutoff()
        expired = [item_id for item_id, start in self._hidden.items() if start < cutoff]
        if not expired:
            return
        for item_id in expired:
            self._hidden.pop(item_id, None)
        await self._store.async_save(self._hidden)
        _LOGGER.debug("Pruned %s expired hidden ids", len(expired))

    async def _async_handle_cleanup(self, _now: datetime) -> None:
        await self._async_prune_hidden()
