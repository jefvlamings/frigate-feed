"""Tests for scaffold contracts (const, models, storage, fixtures)."""

from __future__ import annotations

from custom_components.frigate_feed.const import (
    MEDIA_URL,
    SUPPORTED_DETECTION_LABELS,
    bucket_frigate_label,
    frigate_labels_for_buckets,
)
from custom_components.frigate_feed.models import DetectionItem
from custom_components.frigate_feed.storage import HiddenStore


def test_bucket_frigate_label() -> None:
    assert bucket_frigate_label("person") == "person"
    assert bucket_frigate_label("Bird") == "pet"
    assert bucket_frigate_label("car") == "vehicle"
    assert bucket_frigate_label("package") is None
    assert bucket_frigate_label(None) is None


def test_frigate_labels_for_buckets() -> None:
    assert frigate_labels_for_buckets({"person"}) == ["person"]
    pets = frigate_labels_for_buckets(["pet"])
    assert {"dog", "cat", "bird"} <= set(pets)
    assert "car" not in pets
    assert set(SUPPORTED_DETECTION_LABELS) == {"person", "pet", "vehicle"}


def test_media_url_format() -> None:
    assert MEDIA_URL.format(event_id="1.2-abc", kind="clip.mp4") == "/api/frigate_feed/media/1.2-abc/clip.mp4"


def test_detection_item_round_trip() -> None:
    item = DetectionItem(
        id="1.2-abc",
        start_ts="2026-10-26T10:00:00+01:00",
        end_ts=None,
        duration_s=None,
        label="person",
        source_entity_id="binary_sensor.front_door_person_occupancy",
        camera_name="Front Door",
        snapshot_url=None,
        recording={"status": "pending"},
        frigate={"event_id": "1.2-abc", "label": "person"},
    )
    assert DetectionItem.from_dict(item.as_dict()) == item


async def test_hidden_store_round_trip(hass) -> None:
    store = HiddenStore(hass)
    assert await store.async_load() == {}
    await store.async_save({"1.2-abc": 1.5})
    assert await HiddenStore(hass).async_load() == {"1.2-abc": 1.5}


async def test_fake_frigate_fixture(hass, fake_frigate) -> None:
    runtime = hass.data["frigate"][fake_frigate.entry.entry_id]
    events = await runtime["client"].async_get_events(labels=["person"], limit=10)
    assert events
    assert all(event["label"] == "person" for event in events)
    assert events == sorted(events, key=lambda event: event["start_time"], reverse=True)
