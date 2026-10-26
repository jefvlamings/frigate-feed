"""Tests for the Frigate MQTT live-update listener."""

from __future__ import annotations

from datetime import timedelta
import json
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import (
    async_fire_mqtt_message,
    async_fire_time_changed,
)

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.util import dt as dt_util

from custom_components.frigate_feed.const import MQTT_DEBOUNCE_SECONDS, SIGNAL_FEED_UPDATED
from custom_components.frigate_feed.mqtt_listener import async_setup_mqtt_listener


class FakeSource:
    """Stand-in for FrigateSource exposing only `topic_prefix`."""

    def __init__(self, topic_prefix: str = "frigate") -> None:
        self.topic_prefix = topic_prefix


class FakeManager:
    """Stand-in for FrigateFeedManager counting cache invalidations."""

    def __init__(self) -> None:
        self.invalidations = 0

    def invalidate_cache(self) -> None:
        self.invalidations += 1


def _after(event_id: str = "1700000000.0-abc123", camera: str = "test_cam", label: str = "person") -> dict[str, Any]:
    """Hand-made `after` block shaped like a Frigate 0.14+ tracked object."""
    return {
        "id": event_id,
        "camera": camera,
        "frame_time": 1700000001.5,
        "label": label,
        "sub_label": None,
        "top_score": 0.91,
        "false_positive": False,
        "start_time": 1700000000.0,
        "end_time": None,
        "score": 0.9,
        "box": [10, 20, 110, 220],
        "area": 20000,
        "region": [0, 0, 320, 320],
        "stationary": False,
        "motionless_count": 0,
        "position_changes": 1,
        "current_zones": ["test_zone"],
        "entered_zones": ["test_zone"],
        "has_clip": True,
        "has_snapshot": True,
        "attributes": {},
        "current_attributes": [],
        "max_severity": "alert",
    }


def _payload(event_type: str, **after_kwargs: Any) -> str:
    after = _after(**after_kwargs)
    before = dict(after, current_zones=[], entered_zones=[])
    return json.dumps({"type": event_type, "before": before, "after": after})


@pytest.fixture
def signals(hass: HomeAssistant) -> list[dict[str, Any]]:
    received: list[dict[str, Any]] = []

    @callback
    def _capture(payload: dict[str, Any]) -> None:
        received.append(payload)

    async_dispatcher_connect(hass, SIGNAL_FEED_UPDATED, _capture)
    return received


def _topics(mqtt_mock) -> set[str]:
    return {call.args[0] for call in mqtt_mock.async_subscribe.call_args_list}


async def _advance(hass: HomeAssistant, seconds: float = MQTT_DEBOUNCE_SECONDS + 0.1) -> None:
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


@pytest.mark.parametrize("event_type", ["new", "end"])
async def test_new_and_end_dispatch(
    hass: HomeAssistant, mqtt_mock, signals: list[dict[str, Any]], event_type: str
) -> None:
    manager = FakeManager()
    unsub = await async_setup_mqtt_listener(hass, FakeSource(), manager)
    mqtt_mock.async_subscribe.assert_called()
    assert {"frigate/events", "frigate/reviews"} <= _topics(mqtt_mock)

    async_fire_mqtt_message(hass, "frigate/events", _payload(event_type, label="dog"))
    await hass.async_block_till_done()
    assert signals == []
    assert manager.invalidations == 0

    await _advance(hass)
    assert signals == [
        {"type": event_type, "event_id": "1700000000.0-abc123", "camera": "test_cam", "label": "pet"}
    ]
    assert manager.invalidations == 1
    unsub()


@pytest.mark.parametrize(
    "payload",
    [
        _payload("update"),
        _payload("new", label="umbrella"),
        "not json {",
        json.dumps(["new"]),
        json.dumps({"type": "new", "after": None}),
        "",
    ],
    ids=["update", "unmapped_label", "invalid_json", "not_object", "no_after", "empty"],
)
async def test_ignored_messages(
    hass: HomeAssistant, mqtt_mock, signals: list[dict[str, Any]], payload: str
) -> None:
    manager = FakeManager()
    unsub = await async_setup_mqtt_listener(hass, FakeSource(), manager)

    async_fire_mqtt_message(hass, "frigate/events", payload)
    await hass.async_block_till_done()
    await _advance(hass)

    assert signals == []
    assert manager.invalidations == 0
    unsub()


async def test_debounce_coalesces_burst(
    hass: HomeAssistant, mqtt_mock, signals: list[dict[str, Any]]
) -> None:
    manager = FakeManager()
    unsub = await async_setup_mqtt_listener(hass, FakeSource(), manager)

    async_fire_mqtt_message(hass, "frigate/events", _payload("new", event_id="e1"))
    async_fire_mqtt_message(hass, "frigate/events", _payload("update", event_id="e1"))
    async_fire_mqtt_message(hass, "frigate/events", _payload("new", event_id="e2", label="car"))
    async_fire_mqtt_message(hass, "frigate/events", _payload("end", event_id="e1"))
    await hass.async_block_till_done()
    await _advance(hass)

    assert signals == [{"type": "end", "event_id": "e1", "camera": "test_cam", "label": "person"}]
    assert manager.invalidations == 1

    # A later message opens a new debounce window.
    async_fire_mqtt_message(hass, "frigate/events", _payload("new", event_id="e3", label="car"))
    await hass.async_block_till_done()
    await _advance(hass, MQTT_DEBOUNCE_SECONDS * 2 + 1)
    assert len(signals) == 2
    assert signals[-1]["label"] == "vehicle"
    assert manager.invalidations == 2
    unsub()


async def test_custom_topic_prefix(
    hass: HomeAssistant, mqtt_mock, signals: list[dict[str, Any]]
) -> None:
    manager = FakeManager()
    unsub = await async_setup_mqtt_listener(hass, FakeSource("nvr"), manager)
    assert {"nvr/events", "nvr/reviews"} <= _topics(mqtt_mock)

    async_fire_mqtt_message(hass, "frigate/events", _payload("new"))
    await hass.async_block_till_done()
    await _advance(hass)
    assert signals == []

    async_fire_mqtt_message(hass, "nvr/events", _payload("new"))
    await hass.async_block_till_done()
    await _advance(hass)
    assert len(signals) == 1
    unsub()


async def test_unsubscribe_cancels_pending_timer(
    hass: HomeAssistant, mqtt_mock, signals: list[dict[str, Any]]
) -> None:
    manager = FakeManager()
    unsub = await async_setup_mqtt_listener(hass, FakeSource(), manager)

    async_fire_mqtt_message(hass, "frigate/events", _payload("new"))
    await hass.async_block_till_done()
    unsub()
    await hass.async_block_till_done()
    await _advance(hass)
    assert signals == []
    assert manager.invalidations == 0

    # Subscription is gone too.
    async_fire_mqtt_message(hass, "frigate/events", _payload("new"))
    await hass.async_block_till_done()
    await _advance(hass)
    assert signals == []


async def test_mqtt_unavailable_returns_noop(
    hass: HomeAssistant, signals: list[dict[str, Any]], caplog: pytest.LogCaptureFixture
) -> None:
    manager = FakeManager()
    unsub = await async_setup_mqtt_listener(hass, FakeSource(), manager)

    assert "MQTT is not available" in caplog.text
    unsub()
    await _advance(hass)
    assert signals == []
    assert manager.invalidations == 0


def _review(
    review_type: str,
    before_detections: list[str] | None,
    after_detections: list[str],
    objects: list[str],
    camera: str = "test_cam",
) -> str:
    """Hand-made `<prefix>/reviews` payload shaped like Frigate 0.14+."""

    def _item(detections: list[str]) -> dict[str, Any]:
        return {
            "id": "1700000000.0-rev001",
            "camera": camera,
            "start_time": 1700000000.0,
            "end_time": None,
            "severity": "alert",
            "thumb_path": "/media/frigate/clips/review/thumb.webp",
            "data": {"detections": detections, "objects": objects, "sub_labels": [], "zones": ["test_zone"], "audio": []},
        }

    payload: dict[str, Any] = {"type": review_type, "after": _item(after_detections)}
    payload["before"] = _item(before_detections) if before_detections is not None else _item(after_detections)
    return json.dumps(payload)


async def test_review_new_dispatches(
    hass: HomeAssistant, mqtt_mock, signals: list[dict[str, Any]]
) -> None:
    manager = FakeManager()
    unsub = await async_setup_mqtt_listener(hass, FakeSource(), manager)
    async_fire_mqtt_message(hass, "frigate/reviews", _review("new", None, ["1700000000.0-abc123"], ["person"]))
    await hass.async_block_till_done()
    await _advance(hass)
    assert signals == [
        {"type": "review", "event_id": "1700000000.0-abc123", "camera": "test_cam", "label": "person"}
    ]
    assert manager.invalidations == 1
    unsub()


async def test_review_update_only_when_detection_added(
    hass: HomeAssistant, mqtt_mock, signals: list[dict[str, Any]]
) -> None:
    manager = FakeManager()
    unsub = await async_setup_mqtt_listener(hass, FakeSource(), manager)
    first = "1700000000.0-abc123"
    second = "1700000005.0-def456"
    # Same detections (e.g. thumbnail/zone change): ignored.
    async_fire_mqtt_message(hass, "frigate/reviews", _review("update", [first], [first], ["person"]))
    await hass.async_block_till_done()
    await _advance(hass)
    assert signals == []
    # A second object qualified: refresh, pointing at the added event.
    async_fire_mqtt_message(hass, "frigate/reviews", _review("update", [first], [first, second], ["person", "dog"]))
    await hass.async_block_till_done()
    await _advance(hass)
    assert signals == [{"type": "review", "event_id": second, "camera": "test_cam", "label": "person"}]
    unsub()


async def test_review_ignored_for_end_and_unmapped_objects(
    hass: HomeAssistant, mqtt_mock, signals: list[dict[str, Any]]
) -> None:
    manager = FakeManager()
    unsub = await async_setup_mqtt_listener(hass, FakeSource(), manager)
    async_fire_mqtt_message(hass, "frigate/reviews", _review("end", ["a"], ["a"], ["person"]))
    async_fire_mqtt_message(hass, "frigate/reviews", _review("new", None, ["b"], ["package"]))
    async_fire_mqtt_message(hass, "frigate/reviews", "not json")
    await hass.async_block_till_done()
    await _advance(hass)
    assert signals == []
    assert manager.invalidations == 0
    unsub()
