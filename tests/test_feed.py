"""Tests for the feed manager (event mapping, cache, hidden items)."""

from __future__ import annotations

import copy
from datetime import datetime, timedelta
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
from custom_components.frigate_feed.feed import FrigateFeedManager, event_to_item
from custom_components.frigate_feed.frigate_source import FrigateNotReady
from custom_components.frigate_feed.storage import HiddenStore

IN_PROGRESS_ID = "1793016151.051489-lzerqk"
PERSON_ID = "1793015453.52062-q2c2mg"
BIRD_ID = "1793014800.250000-b1rd01"
CAR_ID = "1793013000.100000-car001"
PACKAGE_ID = "1793012000.500000-pkg001"
DOG_ID = "1793010000.900000-dog001"
OLD_ID = "1792900000.000000-old001"
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

    async def async_get_event(self, event_id: str) -> dict[str, Any] | None:
        try:
            return await self.client.async_get_event(event_id)
        except Exception:
            return None


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


def _event(fake_frigate, event_id: str) -> dict[str, Any]:
    return copy.deepcopy(next(event for event in fake_frigate.events if event["id"] == event_id))


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


async def test_event_to_item_linked(hass: HomeAssistant, fake_frigate, source) -> None:
    event = _event(fake_frigate, PERSON_ID)
    item = event_to_item(event, source)
    assert item is not None
    assert item.id == PERSON_ID
    assert item.label == "person"
    assert item.camera_name == "Front Door"
    assert item.source_entity_id == "binary_sensor.front_door_person_occupancy"
    assert item.snapshot_url == f"/api/frigate_feed/media/{PERSON_ID}/snapshot.jpg"
    assert item.start_dt == dt_util.utc_from_timestamp(event["start_time"])
    assert item.start_dt.utcoffset() == dt_util.as_local(item.start_dt).utcoffset()
    assert item.end_dt == dt_util.utc_from_timestamp(event["end_time"])
    assert item.duration_s == round(event["end_time"] - event["start_time"])

    recording = item.recording
    assert recording["status"] == "linked"
    assert recording["local_url"] == f"/api/frigate_feed/media/{PERSON_ID}/clip.mp4"
    assert recording["start_offset_s"] == 5.0  # front_door alert pre_capture
    clip_start = datetime.fromisoformat(recording["clip_start_ts"])
    clip_end = datetime.fromisoformat(recording["clip_end_ts"])
    assert clip_start == dt_util.utc_from_timestamp(event["start_time"] - 5)
    assert clip_end == dt_util.utc_from_timestamp(event["end_time"] + 8)  # alert post_capture
    assert datetime.fromisoformat(recording["resolved_at"]).tzinfo is not None

    assert item.frigate == {
        "event_id": PERSON_ID,
        "label": "person",
        "sub_label": None,
        "score": 0.83,
        "zones": ["driveway"],
        "camera": "front_door",
    }
    assert set(item.as_dict()) == {
        "id",
        "start_ts",
        "end_ts",
        "duration_s",
        "label",
        "source_entity_id",
        "camera_name",
        "snapshot_url",
        "recording",
        "frigate",
    }


async def test_event_to_item_statuses_and_buckets(hass: HomeAssistant, fake_frigate, source) -> None:
    pending = event_to_item(_event(fake_frigate, IN_PROGRESS_ID), source)
    assert pending.recording == {"status": "pending"}
    assert pending.end_ts is None
    assert pending.duration_s is None

    bird = event_to_item(_event(fake_frigate, BIRD_ID), source)
    assert bird.label == "pet"
    assert bird.frigate["label"] == "bird"
    assert bird.recording["start_offset_s"] == 2.0  # garden_shed detection pre_capture
    assert bird.source_entity_id == ""

    car = event_to_item(_event(fake_frigate, CAR_ID), source)
    assert car.label == "vehicle"
    assert car.recording == {"status": "not_found"}

    dog = event_to_item(_event(fake_frigate, DOG_ID), source)
    assert dog.snapshot_url is None
    assert dog.source_entity_id == "camera.front_door"
    assert dog.frigate["sub_label"] == ["Rex", 0.81]

    assert event_to_item(_event(fake_frigate, PACKAGE_ID), source) is None


async def test_event_to_item_robust_to_missing_keys(hass: HomeAssistant, source) -> None:
    start = time.time() - 60
    item = event_to_item({"id": "x", "label": "cat", "start_time": start, "end_time": start + 3.6}, source)
    assert item is not None
    assert item.label == "pet"
    assert item.duration_s == 4
    assert item.snapshot_url is None
    assert item.recording == {"status": "not_found"}
    assert item.frigate == {
        "event_id": "x",
        "label": "cat",
        "sub_label": None,
        "score": None,
        "zones": [],
        "camera": "",
    }
    assert event_to_item({"label": "person", "start_time": start}, source) is None
    assert event_to_item({"id": "y", "label": "person"}, source) is None


async def test_event_to_item_score_fallbacks(hass: HomeAssistant, source) -> None:
    base = {"id": "s", "label": "person", "start_time": time.time(), "end_time": None}
    assert event_to_item({**base, "top_score": 0.6, "data": {"score": 0.5}}, source).frigate["score"] == 0.6
    assert event_to_item({**base, "data": {"score": 0.5}}, source).frigate["score"] == 0.5


async def test_list_items_default(hass: HomeAssistant, fake_frigate, source) -> None:
    manager = _manager(hass, source)
    await manager.async_start()
    items = await manager.async_list_items()
    assert [item.id for item in items] == [IN_PROGRESS_ID, PERSON_ID, BIRD_ID, CAR_ID, DOG_ID]
    starts = [item.start_dt for item in items]
    assert starts == sorted(starts, reverse=True)

    call = _event_calls(fake_frigate)[-1]
    assert set(call["labels"]) >= {"person", "dog", "bird", "car"}
    assert "package" not in call["labels"]
    assert call["limit"] >= 100
    assert call["after"] == pytest.approx(time.time() - 24 * 3600, abs=5)
    await manager.async_stop()


async def test_list_items_label_filter(hass: HomeAssistant, fake_frigate, source) -> None:
    manager = _manager(hass, source, labels={"person", "pet"})
    await manager.async_start()
    assert [item.id for item in await manager.async_list_items({"pet"})] == [BIRD_ID, DOG_ID]
    # Requested labels are intersected with enabled labels.
    assert await manager.async_list_items({"vehicle"}) == []
    assert {item.label for item in await manager.async_list_items()} == {"person", "pet"}
    assert "car" not in _event_calls(fake_frigate)[-1]["labels"]
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
    """Events older than retention are dropped even if Frigate returns them."""

    async def all_events(*, after: float, limit: int, labels: list[str] | None):
        return copy.deepcopy(fake_frigate.events)

    source.async_get_events = all_events
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
    fake_frigate.client.events = [
        {"id": f"e{i}", "camera": "front_door", "label": "person", "start_time": now - i, "end_time": None}
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
    assert len(_event_calls(fake_frigate)) == 1

    clock[0] += LIST_CACHE_SECONDS + 0.1
    await manager.async_list_items()
    assert len(_event_calls(fake_frigate)) == 2

    manager.invalidate_cache()
    await manager.async_list_items()
    assert len(_event_calls(fake_frigate)) == 3
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
    # Found in cache: no single-event fetch needed.
    assert not [name for name, _ in fake_frigate.client.calls if name == "async_get_event"]

    ids = [item.id for item in await manager.async_list_items()]
    assert PERSON_ID not in ids
    assert len(_event_calls(fake_frigate)) == 2  # cache invalidated by hide

    stored = await HiddenStore(hass).async_load()
    assert stored == {PERSON_ID: pytest.approx(_event(fake_frigate, PERSON_ID)["start_time"])}

    restarted = _manager(hass, source)
    await restarted.async_start()
    assert PERSON_ID not in [item.id for item in await restarted.async_list_items()]
    await manager.async_stop()
    await restarted.async_stop()


async def test_hide_item_uncached_and_unknown(hass: HomeAssistant, fake_frigate, source) -> None:
    manager = _manager(hass, source)
    await manager.async_start()
    await manager.async_hide_item(DOG_ID)
    assert ("async_get_event", {"event_id": DOG_ID}) in fake_frigate.client.calls
    assert DOG_ID not in [item.id for item in await manager.async_list_items()]
    with pytest.raises(ValueError):
        await manager.async_hide_item("missing")
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
    assert (await manager.async_resolve_recording(CAR_ID, final_attempt=True)) == {"status": "not_found"}
    linked = await manager.async_resolve_recording(PERSON_ID)
    assert linked["status"] == "linked"
    assert linked["local_url"].endswith(f"/{PERSON_ID}/clip.mp4")

    # Unmapped labels still yield a recording dict.
    package = await manager.async_resolve_recording(PACKAGE_ID)
    assert package["status"] == "linked"
    assert package["start_offset_s"] == 2.0

    # Event finished since the last list: recording becomes linked.
    in_progress = next(event for event in fake_frigate.client.events if event["id"] == IN_PROGRESS_ID)
    in_progress["end_time"] = in_progress["start_time"] + 2  # ended ~28s ago: clip ready
    assert (await manager.async_resolve_recording(IN_PROGRESS_ID))["status"] == "linked"

    with pytest.raises(ValueError):
        await manager.async_resolve_recording("missing")
    await manager.async_stop()


def _just_ended(fake_frigate, seconds_ago: float) -> dict[str, Any]:
    """The in-progress fixture event, ended `seconds_ago` seconds before now."""
    for event in fake_frigate.events:
        if event["id"] == IN_PROGRESS_ID:
            event["end_time"] = time.time() - seconds_ago
            return copy.deepcopy(event)
    raise AssertionError("fixture event missing")


async def test_recently_ended_clip_stays_pending_until_ready(hass: HomeAssistant, fake_frigate, source) -> None:
    # front_door alert: post_capture 8 + CLIP_READY_GRACE_SECONDS 15 -> ready 23s after end.
    assert event_to_item(_just_ended(fake_frigate, 5), source).recording == {"status": "pending"}
    assert event_to_item(_just_ended(fake_frigate, 30), source).recording["status"] == "linked"


async def test_clip_ready_announces_feed_update(hass: HomeAssistant, fake_frigate, source) -> None:
    from homeassistant.helpers.dispatcher import async_dispatcher_connect

    from custom_components.frigate_feed.const import SIGNAL_FEED_UPDATED

    _just_ended(fake_frigate, 5)
    received: list[dict[str, Any]] = []
    async_dispatcher_connect(hass, SIGNAL_FEED_UPDATED, received.append)
    manager = _manager(hass, source)
    await manager.async_start()

    items = await manager.async_list_items()
    assert next(i for i in items if i.id == IN_PROGRESS_ID).recording == {"status": "pending"}
    await manager.async_list_items()  # cached; must not schedule a second timer
    assert len(manager._clip_ready_timers) == 1

    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
    await hass.async_block_till_done()
    assert received == [
        {"type": "clip_ready", "event_id": IN_PROGRESS_ID, "camera": "front_door", "label": "person"}
    ]
    assert manager._clip_ready_timers == {}
    _just_ended(fake_frigate, 60)  # HA timers moved on; move the event's real end time too
    items = await manager.async_list_items()
    assert next(i for i in items if i.id == IN_PROGRESS_ID).recording["status"] == "linked"
    await manager.async_stop()


async def test_stop_cancels_clip_ready_timers(hass: HomeAssistant, fake_frigate, source) -> None:
    from homeassistant.helpers.dispatcher import async_dispatcher_connect

    from custom_components.frigate_feed.const import SIGNAL_FEED_UPDATED

    _just_ended(fake_frigate, 5)
    received: list[dict[str, Any]] = []
    async_dispatcher_connect(hass, SIGNAL_FEED_UPDATED, received.append)
    manager = _manager(hass, source)
    await manager.async_start()
    await manager.async_list_items()
    await manager.async_stop()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=30))
    await hass.async_block_till_done()
    assert received == []
