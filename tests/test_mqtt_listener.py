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


REVIEW_ID = "1700000000.0-rev001"


def _item(detections: list[str], objects: list[str], camera: str = "test_cam") -> dict[str, Any]:
    """Hand-made review item shaped like Frigate 0.14+."""
    return {
        "id": REVIEW_ID,
        "camera": camera,
        "start_time": 1700000000.0,
        "end_time": None,
        "severity": "alert",
        "thumb_path": "/media/frigate/clips/review/thumb.webp",
        "data": {"detections": detections, "objects": objects, "sub_labels": [], "zones": ["test_zone"], "audio": []},
    }


def _review(
    review_type: str,
    after_detections: list[str],
    objects: list[str],
    *,
    before_detections: list[str] | None = None,
    before_objects: list[str] | None = None,
    camera: str = "test_cam",
) -> str:
    """Hand-made `<prefix>/reviews` payload; `before` defaults to `after`."""
    payload = {
        "type": review_type,
        "before": _item(
            after_detections if before_detections is None else before_detections,
            objects if before_objects is None else before_objects,
            camera,
        ),
        "after": _item(after_detections, objects, camera),
    }
    return json.dumps(payload)


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


@pytest.mark.parametrize("review_type", ["new", "end"])
async def test_new_and_end_dispatch(
    hass: HomeAssistant, mqtt_mock, signals: list[dict[str, Any]], review_type: str
) -> None:
    manager = FakeManager()
    unsub = await async_setup_mqtt_listener(hass, FakeSource(), manager)
    mqtt_mock.async_subscribe.assert_called()
    assert "frigate/reviews" in _topics(mqtt_mock)
    assert "frigate/events" not in _topics(mqtt_mock)

    async_fire_mqtt_message(hass, "frigate/reviews", _review(review_type, ["e1"], ["package", "dog"]))
    await hass.async_block_till_done()
    assert signals == []
    assert manager.invalidations == 0

    await _advance(hass)
    assert signals == [{"type": review_type, "review_id": REVIEW_ID, "camera": "test_cam", "label": "pet"}]
    assert manager.invalidations == 1
    unsub()


@pytest.mark.parametrize(
    "payload",
    [
        _review("update", ["e1"], ["person"]),
        _review("new", ["e1"], ["umbrella"]),
        _review("genai", ["e1"], ["person"]),
        "not json {",
        json.dumps(["new"]),
        json.dumps({"type": "new", "after": None}),
        "",
    ],
    ids=["unchanged_update", "unmapped_label", "other_type", "invalid_json", "not_object", "no_after", "empty"],
)
async def test_ignored_messages(
    hass: HomeAssistant, mqtt_mock, signals: list[dict[str, Any]], payload: str
) -> None:
    manager = FakeManager()
    unsub = await async_setup_mqtt_listener(hass, FakeSource(), manager)

    async_fire_mqtt_message(hass, "frigate/reviews", payload)
    await hass.async_block_till_done()
    await _advance(hass)

    assert signals == []
    assert manager.invalidations == 0
    unsub()


async def test_update_dispatches_when_detection_or_object_added(
    hass: HomeAssistant, mqtt_mock, signals: list[dict[str, Any]]
) -> None:
    manager = FakeManager()
    unsub = await async_setup_mqtt_listener(hass, FakeSource(), manager)

    async_fire_mqtt_message(
        hass, "frigate/reviews", _review("update", ["e1", "e2"], ["person"], before_detections=["e1"])
    )
    await hass.async_block_till_done()
    await _advance(hass)
    assert signals == [{"type": "update", "review_id": REVIEW_ID, "camera": "test_cam", "label": "person"}]

    async_fire_mqtt_message(
        hass, "frigate/reviews", _review("update", ["e1"], ["car", "person"], before_objects=["person"])
    )
    await hass.async_block_till_done()
    await _advance(hass)
    assert len(signals) == 2
    assert signals[-1]["label"] == "vehicle"
    unsub()


async def test_debounce_coalesces_burst(
    hass: HomeAssistant, mqtt_mock, signals: list[dict[str, Any]]
) -> None:
    manager = FakeManager()
    unsub = await async_setup_mqtt_listener(hass, FakeSource(), manager)

    async_fire_mqtt_message(hass, "frigate/reviews", _review("new", ["e1"], ["person"]))
    async_fire_mqtt_message(hass, "frigate/reviews", _review("update", ["e1"], ["person"]))
    async_fire_mqtt_message(hass, "frigate/reviews", _review("update", ["e1", "e2"], ["person"], before_detections=["e1"]))
    async_fire_mqtt_message(hass, "frigate/reviews", _review("end", ["e1", "e2"], ["person"]))
    await hass.async_block_till_done()
    await _advance(hass)

    assert signals == [{"type": "end", "review_id": REVIEW_ID, "camera": "test_cam", "label": "person"}]
    assert manager.invalidations == 1

    # A later message opens a new debounce window.
    async_fire_mqtt_message(hass, "frigate/reviews", _review("new", ["e3"], ["car"]))
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
    assert "nvr/reviews" in _topics(mqtt_mock)

    async_fire_mqtt_message(hass, "frigate/reviews", _review("new", ["e1"], ["person"]))
    await hass.async_block_till_done()
    await _advance(hass)
    assert signals == []

    async_fire_mqtt_message(hass, "nvr/reviews", _review("new", ["e1"], ["person"]))
    await hass.async_block_till_done()
    await _advance(hass)
    assert len(signals) == 1
    unsub()


async def test_unsubscribe_cancels_pending_timer(
    hass: HomeAssistant, mqtt_mock, signals: list[dict[str, Any]]
) -> None:
    manager = FakeManager()
    unsub = await async_setup_mqtt_listener(hass, FakeSource(), manager)

    async_fire_mqtt_message(hass, "frigate/reviews", _review("new", ["e1"], ["person"]))
    await hass.async_block_till_done()
    unsub()
    await hass.async_block_till_done()
    await _advance(hass)
    assert signals == []
    assert manager.invalidations == 0

    # Subscription is gone too.
    async_fire_mqtt_message(hass, "frigate/reviews", _review("new", ["e1"], ["person"]))
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
