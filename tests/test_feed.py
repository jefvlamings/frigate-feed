"""Tests for the feed manager (review item mapping, cache, hidden items)."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta
import math
import time
from types import SimpleNamespace
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.frigate_feed import feed as feed_module
from custom_components.frigate_feed.const import (
    CLEANUP_INTERVAL_SECONDS,
    DEFAULT_PRE_CAPTURE_SECONDS,
    LIST_CACHE_SECONDS,
    LIST_ITEMS_LIMIT,
)
from custom_components.frigate_feed.feed import FrigateFeedManager, review_to_item
from custom_components.frigate_feed.frigate_source import FrigateNotReady
from custom_components.frigate_feed.storage import HiddenStore

# Review items (feed item ids)
IN_PROGRESS_ID = "1793016151.200000-rev001"
PERSON_ID = "1793015453.600000-rev002"
BIRD_ID = "1793014800.300000-rev003"
CAR_ID = "1793012000.600000-rev004"  # car + package
PACKAGE_ID = "1793011000.000000-rev005"  # package only
DOG_ID = "1793010001.000000-rev006"
OLD_ID = "1792900000.100000-rev007"
# Frigate events (detections)
PERSON_EVENT = "1793015453.52062-q2c2mg"
CAR_EVENT = "1793013000.100000-car001"
PACKAGE_EVENT = "1793012000.500000-pkg001"
ALL_LABELS = {"person", "pet", "vehicle"}


class FakeSource:
    """Local stand-in implementing the FrigateSource method contract."""

    def __init__(self, client: Any, config: dict[str, Any]) -> None:
        self.client = client
        self.config = config
        self.entity_ids = {
            ("front_door", "person"): "binary_sensor.front_door_person_occupancy",
            ("front_door", None): "camera.front_door",
        }

    def camera_display_name(self, camera: str) -> str:
        return camera.replace("_", " ").title()

    def pre_capture_seconds(self, camera: str, severity: str | None = None) -> float:
        return self._capture(camera, severity, "pre_capture")

    def post_capture_seconds(self, camera: str, severity: str | None = None) -> float:
        return self._capture(camera, severity, "post_capture")

    def _capture(self, camera: str, severity: str | None, field: str) -> float:
        record = self.config.get("cameras", {}).get(camera, {}).get("record", {})
        key = "alerts" if severity == "alert" else "detections"
        value = record.get(key, {}).get(field)
        return float(value) if value is not None else float(DEFAULT_PRE_CAPTURE_SECONDS)

    def source_entity_id(self, camera: str, frigate_label: str) -> str | None:
        return self.entity_ids.get((camera, frigate_label)) or self.entity_ids.get((camera, None))

    async def async_get_events(
        self, *, after: float, limit: int, labels: list[str] | None
    ) -> list[dict[str, Any]]:
        try:
            return await self.client.async_get_events(after=int(after), limit=limit, labels=labels)
        except Exception as err:
            raise FrigateNotReady(str(err)) from err

    async def async_get_reviews(
        self, *, after: float, limit: int, labels: list[str] | None
    ) -> list[dict[str, Any]]:
        try:
            return await self.client.async_get_reviews(after=int(after), limit=limit, labels=labels)
        except Exception as err:
            raise FrigateNotReady(str(err)) from err

    async def async_get_review(self, review_id: str) -> dict[str, Any] | None:
        try:
            return await self.client.api_wrapper("get", f"http://frigate.test:5000/api/review/{review_id}")
        except Exception:
            return None

    def recording_enabled(self, camera: str) -> bool:
        record = self.config.get("cameras", {}).get(camera, {}).get("record", {})
        return record.get("enabled") is not False


@pytest.fixture
def source(fake_frigate) -> FakeSource:
    return FakeSource(fake_frigate.client, fake_frigate.config)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Controllable monotonic clock for the feed cache."""
    now = [1000.0]
    monkeypatch.setattr(
        feed_module, "time", SimpleNamespace(time=time.time, monotonic=lambda: now[0])
    )
    return now


def _review(fake_frigate, review_id: str) -> dict[str, Any]:
    return copy.deepcopy(next(review for review in fake_frigate.reviews if review["id"] == review_id))


def _events(fake_frigate) -> dict[str, dict[str, Any]]:
    return {event["id"]: copy.deepcopy(event) for event in fake_frigate.events}


def _item(fake_frigate, source, review_id: str, wanted: set[str] | None = None):
    return review_to_item(_review(fake_frigate, review_id), source, _events(fake_frigate), wanted)


def _manager(
    hass: HomeAssistant,
    source: FakeSource,
    *,
    labels: set[str] = ALL_LABELS,
    retention_hours: int = 24,
    max_detections: int = 100,
) -> FrigateFeedManager:
    return FrigateFeedManager(hass, source, HiddenStore(hass), labels, retention_hours, max_detections)


def _event_calls(fake_frigate) -> list[dict[str, Any]]:
    return [args for name, args in fake_frigate.client.calls if name == "async_get_events"]


def _review_calls(fake_frigate) -> list[dict[str, Any]]:
    return [args for name, args in fake_frigate.client.calls if name == "async_get_reviews"]


async def test_review_to_item_linked(hass: HomeAssistant, fake_frigate, source) -> None:
    review = _review(fake_frigate, PERSON_ID)
    item = _item(fake_frigate, source, PERSON_ID)
    assert item is not None
    assert item.id == PERSON_ID
    assert item.label == "person"
    assert item.labels == ["person"]
    assert item.camera_name == "Front Door"
    assert item.source_entity_id == "binary_sensor.front_door_person_occupancy"
    # Media of an ended review item is versioned by its end time (browser-cacheable).
    version = int(review["end_time"])
    assert item.snapshot_url == f"/api/frigate_feed/media/{PERSON_EVENT}/snapshot.jpg?v={version}"
    assert item.thumbnail_url == (
        f"/api/frigate_feed/media/{PERSON_EVENT}/snapshot.jpg?crop=1&bbox=0&height=400&quality=80&v={version}"
    )
    assert item.start_dt == dt_util.utc_from_timestamp(review["start_time"])
    assert item.start_dt.utcoffset() == dt_util.as_local(item.start_dt).utcoffset()
    assert item.end_dt == dt_util.utc_from_timestamp(review["end_time"])
    assert item.duration_s == round(review["end_time"] - review["start_time"])

    recording = item.recording
    clip_start = math.floor(review["start_time"] - 5)  # front_door alert pre_capture
    clip_end = math.ceil(review["end_time"] + 8)  # front_door alert post_capture
    assert recording["status"] == "linked"
    assert recording["local_url"] == f"/api/frigate_feed/media/review/{PERSON_ID}/clip.mp4"
    assert recording["start_offset_s"] == pytest.approx(review["start_time"] - clip_start, abs=1e-3)
    assert datetime.fromisoformat(recording["clip_start_ts"]) == dt_util.utc_from_timestamp(clip_start)
    assert datetime.fromisoformat(recording["clip_end_ts"]) == dt_util.utc_from_timestamp(clip_end)
    assert datetime.fromisoformat(recording["resolved_at"]).tzinfo is not None

    event = next(event for event in fake_frigate.events if event["id"] == PERSON_EVENT)
    assert item.detections == [
        {
            "event_id": PERSON_EVENT,
            "label": "person",
            "frigate_label": "person",
            "sub_label": None,
            "score": 0.83,
            "start_offset_s": pytest.approx(event["start_time"] - clip_start, abs=1e-3),
            "duration_s": round(event["end_time"] - event["start_time"]),
            "has_snapshot": True,
        }
    ]
    assert item.frigate == {
        "review_id": PERSON_ID,
        "event_id": PERSON_EVENT,
        "severity": "alert",
        "camera": "front_door",
        "label": "person",
        "sub_label": None,
        "score": 0.83,
        "box": [0.58, 0.52, 0.02, 0.07],
        "objects": ["person"],
        "sub_labels": [],
        "zones": ["driveway"],
    }
    assert set(item.as_dict()) == {
        "id",
        "start_ts",
        "end_ts",
        "duration_s",
        "label",
        "labels",
        "source_entity_id",
        "camera_name",
        "snapshot_url",
        "thumbnail_url",
        "recording",
        "detections",
        "frigate",
    }


async def test_review_to_item_statuses_and_buckets(hass: HomeAssistant, fake_frigate, source) -> None:
    pending = _item(fake_frigate, source, IN_PROGRESS_ID)
    assert pending.recording == {"status": "pending"}
    assert pending.end_ts is None
    assert pending.duration_s is None
    assert "v=" not in pending.snapshot_url
    assert "v=" not in pending.thumbnail_url

    bird = _item(fake_frigate, source, BIRD_ID)
    assert bird.label == "pet"
    assert bird.frigate["label"] == "bird"
    assert bird.recording["start_offset_s"] == pytest.approx(2.0, abs=1.0)  # detection pre_capture 2
    assert bird.source_entity_id == ""

    # car + package: the unmapped package is neither a label nor a detection, and
    # the recording comes from the camera recordings even though the car event has no clip.
    car = _item(fake_frigate, source, CAR_ID)
    assert car.labels == ["vehicle"]
    assert [detection["event_id"] for detection in car.detections] == [CAR_EVENT]
    car_version = int(_review(fake_frigate, CAR_ID)["end_time"])
    assert car.snapshot_url == f"/api/frigate_feed/media/{CAR_EVENT}/snapshot.jpg?v={car_version}"
    assert car.recording["status"] == "linked"

    # No detection with a snapshot: fall back to the review thumbnail.
    dog = _item(fake_frigate, source, DOG_ID)
    dog_version = int(_review(fake_frigate, DOG_ID)["end_time"])
    assert dog.snapshot_url == f"/api/frigate_feed/media/review/{DOG_ID}/thumb.webp?v={dog_version}"
    assert dog.thumbnail_url is None
    assert dog.source_entity_id == "camera.front_door"
    assert dog.frigate["sub_labels"] == ["Rex"]
    assert dog.frigate["event_id"] is None

    assert _item(fake_frigate, source, PACKAGE_ID) is None
    assert _item(fake_frigate, source, PERSON_ID, {"pet"}) is None


async def test_review_to_item_groups_detections(hass: HomeAssistant, fake_frigate, source) -> None:
    """Several detections of different labels in one review item become one feed item."""
    start = time.time() - 120
    review = {
        "id": "100.0-group1",
        "camera": "front_door",
        "start_time": start,
        "end_time": start + 40,
        "severity": "alert",
        "data": {"detections": ["e-dog", "e-person", "e-car", "e-missing"], "objects": ["dog", "person", "car-verified"]},
    }
    events = {
        "e-person": {"id": "e-person", "label": "person", "start_time": start + 10, "end_time": start + 30,
                     "has_snapshot": True, "data": {"top_score": 0.7}},
        "e-dog": {"id": "e-dog", "label": "dog", "start_time": start - 2, "end_time": None,
                  "has_snapshot": True, "data": {"top_score": 0.95}},
        "e-car": {"id": "e-car", "label": "car", "start_time": start + 20, "end_time": start + 25,
                  "has_snapshot": True, "data": {"top_score": 0.99}},
    }
    item = review_to_item(review, source, events)
    assert item.labels == ["person", "vehicle", "pet"]
    assert item.label == "person"
    # The best snapshot prefers the main label over a higher score.
    version = int(start + 40)
    assert item.snapshot_url == f"/api/frigate_feed/media/e-person/snapshot.jpg?v={version}"
    assert item.frigate["event_id"] == "e-person"
    clip_start = math.floor(start - 5)
    assert [(d["event_id"], d["label"]) for d in item.detections] == [
        ("e-dog", "pet"),
        ("e-person", "person"),
        ("e-car", "vehicle"),
    ]
    assert item.detections[0]["start_offset_s"] == pytest.approx(start - 2 - clip_start, abs=1e-3)
    assert item.detections[0]["duration_s"] is None

    # Only wanted labels count: the main label and snapshot follow.
    pets_and_cars = review_to_item(review, source, events, {"pet", "vehicle"})
    assert pets_and_cars.labels == ["vehicle", "pet"]
    assert pets_and_cars.snapshot_url == f"/api/frigate_feed/media/e-car/snapshot.jpg?v={version}"
    assert [d["event_id"] for d in pets_and_cars.detections] == ["e-dog", "e-car"]

    # Without event details the review objects still give the labels.
    bare = review_to_item(review, source, {})
    assert bare.labels == ["person", "vehicle", "pet"]
    assert bare.detections == []
    assert bare.snapshot_url == f"/api/frigate_feed/media/review/100.0-group1/thumb.webp?v={version}"


async def test_review_to_item_robust_to_missing_keys(hass: HomeAssistant, source) -> None:
    start = time.time() - 60
    item = review_to_item(
        {"id": "x", "start_time": start, "end_time": start + 3.6, "data": {"objects": ["cat"]}}, source, {}
    )
    assert item is not None
    assert item.label == "pet"
    assert item.duration_s == 4
    assert item.recording["status"] == "linked"
    assert item.frigate["camera"] == ""
    assert item.frigate["zones"] == []
    assert review_to_item({"start_time": start, "data": {"objects": ["person"]}}, source, {}) is None
    assert review_to_item({"id": "y", "data": {"objects": ["person"]}}, source, {}) is None
    assert review_to_item({"id": "z", "start_time": start}, source, {}) is None


async def test_review_to_item_recording_disabled(hass: HomeAssistant, fake_frigate, source) -> None:
    fake_frigate.config["cameras"]["front_door"]["record"]["enabled"] = False
    assert _item(fake_frigate, source, PERSON_ID).recording == {"status": "not_found"}


async def test_list_items_default(hass: HomeAssistant, fake_frigate, source) -> None:
    manager = _manager(hass, source)
    await manager.async_start()
    items = await manager.async_list_items()
    assert [item.id for item in items] == [IN_PROGRESS_ID, PERSON_ID, BIRD_ID, CAR_ID, DOG_ID]
    starts = [item.start_dt for item in items]
    assert starts == sorted(starts, reverse=True)

    call = _review_calls(fake_frigate)[-1]
    assert set(call["labels"]) >= {"person", "dog", "bird", "car"}
    assert "package" not in call["labels"]
    assert call["limit"] >= 100
    assert call["after"] == pytest.approx(time.time() - 24 * 3600, abs=5)
    # Detection details come from one events query reaching 10 minutes before the
    # oldest review item (an event can start before its review item).
    events_call = _event_calls(fake_frigate)[-1]
    assert events_call["labels"] == call["labels"]
    assert events_call["after"] == pytest.approx(_review(fake_frigate, DOG_ID)["start_time"] - 600, abs=1)
    await manager.async_stop()


async def test_list_items_label_filter(hass: HomeAssistant, fake_frigate, source) -> None:
    manager = _manager(hass, source, labels={"person", "pet"})
    await manager.async_start()
    assert [item.id for item in await manager.async_list_items({"pet"})] == [BIRD_ID, DOG_ID]
    # Requested labels are intersected with enabled labels.
    assert await manager.async_list_items({"vehicle"}) == []
    assert {item.label for item in await manager.async_list_items()} == {"person", "pet"}
    assert "car" not in _review_calls(fake_frigate)[-1]["labels"]
    await manager.async_stop()


async def test_list_items_retention(hass: HomeAssistant, fake_frigate, source) -> None:
    manager = _manager(hass, source, retention_hours=48)
    await manager.async_start()
    ids = [item.id for item in await manager.async_list_items()]
    assert ids[-1] == OLD_ID

    short = _manager(hass, source, retention_hours=1)
    await short.async_start()
    ids = [item.id for item in await short.async_list_items()]
    assert OLD_ID not in ids and DOG_ID not in ids
    await manager.async_stop()
    await short.async_stop()


async def test_list_items_filters_out_of_retention_events_from_source(
    hass: HomeAssistant, fake_frigate, source
) -> None:
    """Review items older than retention are dropped even if Frigate returns them."""

    async def all_reviews(*, after: float, limit: int, labels: list[str] | None):
        return copy.deepcopy(fake_frigate.reviews)

    source.async_get_reviews = all_reviews
    manager = _manager(hass, source)
    await manager.async_start()
    ids = [item.id for item in await manager.async_list_items()]
    assert OLD_ID not in ids
    assert PACKAGE_ID not in ids
    await manager.async_stop()


async def test_list_items_max_detections(hass: HomeAssistant, fake_frigate, source) -> None:
    manager = _manager(hass, source, max_detections=2)
    await manager.async_start()
    assert [item.id for item in await manager.async_list_items()] == [IN_PROGRESS_ID, PERSON_ID]
    await manager.async_stop()


async def test_list_items_limit_capped(hass: HomeAssistant, fake_frigate, source) -> None:
    now = time.time()
    fake_frigate.client.reviews = [
        {"id": f"e{i}", "camera": "front_door", "start_time": now - i, "end_time": None,
         "data": {"objects": ["person"], "detections": []}}
        for i in range(LIST_ITEMS_LIMIT + 20)
    ]
    manager = _manager(hass, source, max_detections=2000)
    await manager.async_start()
    items = await manager.async_list_items()
    assert len(items) == LIST_ITEMS_LIMIT
    assert items[0].id == "e0"
    await manager.async_stop()


async def test_list_items_cache(hass: HomeAssistant, fake_frigate, source, clock) -> None:
    manager = _manager(hass, source)
    await manager.async_start()
    await manager.async_list_items()
    await manager.async_list_items({"person"})
    assert len(_review_calls(fake_frigate)) == 1
    assert len(_event_calls(fake_frigate)) == 1

    clock[0] += LIST_CACHE_SECONDS + 0.1
    await manager.async_list_items()
    assert len(_review_calls(fake_frigate)) == 2

    manager.invalidate_cache()
    await manager.async_list_items()
    assert len(_review_calls(fake_frigate)) == 3
    await manager.async_stop()


async def test_list_items_frigate_not_ready(hass: HomeAssistant, fake_frigate, source) -> None:
    manager = _manager(hass, source)
    await manager.async_start()
    fake_frigate.client.fail = True
    with pytest.raises(FrigateNotReady):
        await manager.async_list_items()
    await manager.async_stop()


async def test_hide_item_persists(hass: HomeAssistant, fake_frigate, source, clock) -> None:
    manager = _manager(hass, source)
    await manager.async_start()
    await manager.async_list_items()
    await manager.async_hide_item(PERSON_ID)
    # Found in cache: no single-review fetch needed.
    assert not [name for name, _ in fake_frigate.client.calls if name == "api_wrapper"]

    ids = [item.id for item in await manager.async_list_items()]
    assert PERSON_ID not in ids
    assert len(_review_calls(fake_frigate)) == 2  # cache invalidated by hide

    stored = await HiddenStore(hass).async_load()
    assert stored == {PERSON_ID: pytest.approx(_review(fake_frigate, PERSON_ID)["start_time"])}

    restarted = _manager(hass, source)
    await restarted.async_start()
    assert PERSON_ID not in [item.id for item in await restarted.async_list_items()]
    await manager.async_stop()
    await restarted.async_stop()


async def test_hide_item_uncached_and_unknown(hass: HomeAssistant, fake_frigate, source) -> None:
    manager = _manager(hass, source)
    await manager.async_start()
    await manager.async_hide_item(DOG_ID)
    assert (
        "api_wrapper",
        {"method": "get", "url": f"http://frigate.test:5000/api/review/{DOG_ID}"},
    ) in fake_frigate.client.calls
    assert DOG_ID not in [item.id for item in await manager.async_list_items()]
    with pytest.raises(ValueError):
        await manager.async_hide_item("missing")
    await manager.async_stop()


async def test_items_hidden_by_event_id_stay_hidden(hass: HomeAssistant, fake_frigate, source) -> None:
    """Ids hidden before the feed used review items are event ids: hide their review item."""
    await HiddenStore(hass).async_save({PERSON_EVENT: time.time() - 60})
    manager = _manager(hass, source)
    await manager.async_start()
    ids = [item.id for item in await manager.async_list_items()]
    assert PERSON_ID not in ids
    assert IN_PROGRESS_ID in ids
    await manager.async_stop()


async def test_hidden_prune(hass: HomeAssistant, fake_frigate, source) -> None:
    now = time.time()
    await HiddenStore(hass).async_save({"stale": now - 48 * 3600, PERSON_ID: now - 60})
    manager = _manager(hass, source)
    await manager.async_start()
    assert await HiddenStore(hass).async_load() == {PERSON_ID: pytest.approx(now - 60)}

    # Shrink retention so the hourly prune drops the remaining id.
    manager._retention_hours = 0
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=CLEANUP_INTERVAL_SECONDS + 1))
    await hass.async_block_till_done()
    assert await HiddenStore(hass).async_load() == {}

    await manager.async_stop()
    await HiddenStore(hass).async_save({"again": time.time() - 3600})
    manager._hidden = {"again": time.time() - 3600}
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=2 * CLEANUP_INTERVAL_SECONDS + 2))
    await hass.async_block_till_done()
    assert await HiddenStore(hass).async_load() == {"again": pytest.approx(time.time() - 3600, abs=5)}


async def test_resolve_recording(hass: HomeAssistant, fake_frigate, source) -> None:
    manager = _manager(hass, source)
    await manager.async_start()
    assert (await manager.async_resolve_recording(IN_PROGRESS_ID)) == {"status": "pending"}
    linked = await manager.async_resolve_recording(PERSON_ID)
    assert linked["status"] == "linked"
    assert linked["local_url"] == f"/api/frigate_feed/media/review/{PERSON_ID}/clip.mp4"

    # Unmapped labels still yield a recording dict.
    package = await manager.async_resolve_recording(PACKAGE_ID)
    assert package["status"] == "linked"

    fake_frigate.config["cameras"]["garden_shed"]["record"]["enabled"] = False
    assert (await manager.async_resolve_recording(CAR_ID, final_attempt=True)) == {"status": "not_found"}

    # Review item finished since the last list: recording becomes linked.
    in_progress = next(review for review in fake_frigate.client.reviews if review["id"] == IN_PROGRESS_ID)
    in_progress["end_time"] = in_progress["start_time"] + 2  # ended ~28s ago: clip ready
    assert (await manager.async_resolve_recording(IN_PROGRESS_ID))["status"] == "linked"

    with pytest.raises(ValueError):
        await manager.async_resolve_recording("missing")
    await manager.async_stop()


def _just_ended(fake_frigate, seconds_ago: float) -> dict[str, Any]:
    """The in-progress fixture review item, ended `seconds_ago` seconds before now."""
    for review in fake_frigate.reviews:
        if review["id"] == IN_PROGRESS_ID:
            review["end_time"] = time.time() - seconds_ago
            return copy.deepcopy(review)
    raise AssertionError("fixture review item missing")


async def test_recently_ended_clip_stays_pending_until_ready(hass: HomeAssistant, fake_frigate, source) -> None:
    # front_door alert: post_capture 8 + CLIP_READY_GRACE_SECONDS 15 -> ready 23s after end.
    events = _events(fake_frigate)
    assert review_to_item(_just_ended(fake_frigate, 5), source, events).recording == {"status": "pending"}
    assert review_to_item(_just_ended(fake_frigate, 30), source, events).recording["status"] == "linked"


async def test_review_to_item_snapshot_box(hass: HomeAssistant, source) -> None:
    """The card centres the thumbnail crop on the snapshot box; invalid boxes are dropped."""
    start = time.time() - 60
    review = {"id": "200.0-box1", "camera": "front_door", "start_time": start, "end_time": start + 5,
              "data": {"detections": ["e1"], "objects": ["person"]}}

    def item_box(box: Any) -> Any:
        event = {"id": "e1", "label": "person", "start_time": start, "end_time": start + 5,
                 "has_snapshot": True, "data": {"box": box}}
        return review_to_item(review, source, {"e1": event}).frigate["box"]

    assert item_box([0.123456, 0.2, 0.3, 0.4]) == [0.1235, 0.2, 0.3, 0.4]
    for bad in (None, [0.1, 0.2, 0.3], [0.1, 0.2, 0.3, 1.5], ["a", 0, 0, 0], [0.1, 0.2, 0.3, True]):
        assert item_box(bad) is None, bad
    assert review_to_item(review, source, {}).frigate["box"] is None
