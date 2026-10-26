"""MQTT listener that pushes live feed updates."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import json
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later

from .const import MQTT_DEBOUNCE_SECONDS, SIGNAL_FEED_UPDATED, bucket_frigate_label
from .frigate_source import FrigateSource

if TYPE_CHECKING:
    from homeassistant.components.mqtt import ReceiveMessage

    from .feed import FrigateFeedManager

_LOGGER = logging.getLogger(__name__)

MQTT_DOMAIN = "mqtt"
MQTT_WAIT_TIMEOUT_SECONDS = 10.0
LIVE_EVENT_TYPES: frozenset[str] = frozenset({"new", "end"})


def _noop() -> None:
    """Unsubscribe placeholder used when MQTT is unavailable."""


async def _async_mqtt_available(hass: HomeAssistant) -> bool:
    """Return True when HA's MQTT client is (or becomes, shortly) available."""
    if not hass.config_entries.async_entries(MQTT_DOMAIN):
        return False
    # Local import: mqtt is only an after_dependency and may not be installed/used.
    from homeassistant.components.mqtt.util import async_wait_for_mqtt_client

    try:
        async with asyncio.timeout(MQTT_WAIT_TIMEOUT_SECONDS):
            return await async_wait_for_mqtt_client(hass)
    except TimeoutError:
        return False


def _decode_payload(payload: Any) -> dict[str, Any] | None:
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError:
            return None
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def parse_event_message(payload: Any) -> dict[str, Any] | None:
    """Turn a raw `<prefix>/events` payload into a feed update, or None to ignore it."""
    data = _decode_payload(payload)
    if data is None:
        return None
    event_type = data.get("type")
    if event_type not in LIVE_EVENT_TYPES:
        return None
    after = data.get("after")
    if not isinstance(after, dict):
        return None
    bucket = bucket_frigate_label(after.get("label"))
    if bucket is None:
        return None
    return {
        "type": event_type,
        "event_id": after.get("id"),
        "camera": after.get("camera"),
        "label": bucket,
    }


def _review_detections(review: Any) -> list[str]:
    data = review.get("data") if isinstance(review, dict) else None
    detections = data.get("detections") if isinstance(data, dict) else None
    return [str(event_id) for event_id in detections] if isinstance(detections, list) else []


def parse_review_message(payload: Any) -> dict[str, Any] | None:
    """Turn a raw `<prefix>/reviews` payload into a feed update, or None to ignore it.

    The feed only lists events Frigate classified as an alert or detection, which
    usually happens after the event started (e.g. when the object enters a required
    zone). Frigate announces that on the reviews topic: a `new` review item, or an
    `update` that adds a detection to an existing one.
    """
    data = _decode_payload(payload)
    if data is None:
        return None
    review_type = data.get("type")
    after = data.get("after")
    if review_type not in ("new", "update") or not isinstance(after, dict):
        return None
    detections = _review_detections(after)
    added = [event_id for event_id in detections if event_id not in _review_detections(data.get("before"))]
    if review_type == "update" and not added:
        return None
    review_data = after.get("data")
    objects = review_data.get("objects") if isinstance(review_data, dict) else None
    buckets = [bucket_frigate_label(obj) for obj in objects] if isinstance(objects, list) else []
    bucket = next((bucket for bucket in buckets if bucket), None)
    if bucket is None:
        return None
    return {
        "type": "review",
        "event_id": (added or detections or [None])[-1],
        "camera": after.get("camera"),
        "label": bucket,
    }


async def async_setup_mqtt_listener(
    hass: HomeAssistant, source: FrigateSource, manager: "FrigateFeedManager"
) -> Callable[[], None]:
    """Subscribe to f"{source.topic_prefix}/events" and f"{source.topic_prefix}/reviews".

    Returns an unsubscribe callable. For `new` and `end` event messages whose
    `after.label` maps onto a feed bucket, and for review messages that add a
    detection (see parse_review_message):
    debounce MQTT_DEBOUNCE_SECONDS, then call manager.invalidate_cache() and
    async_dispatcher_send(hass, SIGNAL_FEED_UPDATED,
        {"type": ..., "event_id": ..., "camera": ..., "label": <bucket>}).
    `update` messages and unmapped labels are ignored. When MQTT is not set up,
    log a warning and return a no-op unsubscribe.
    """
    topic = f"{source.topic_prefix}/events"
    review_topic = f"{source.topic_prefix}/reviews"
    if not await _async_mqtt_available(hass):
        _LOGGER.warning("MQTT is not available; live feed updates from %s are disabled", topic)
        return _noop

    from homeassistant.components import mqtt

    pending: dict[str, Any] = {}
    cancel_timer: CALLBACK_TYPE | None = None

    @callback
    def _fire(_now: Any) -> None:
        nonlocal cancel_timer
        cancel_timer = None
        update = dict(pending)
        pending.clear()
        if not update:
            return
        manager.invalidate_cache()
        async_dispatcher_send(hass, SIGNAL_FEED_UPDATED, update)

    @callback
    def _queue(update: dict[str, Any] | None) -> None:
        nonlocal cancel_timer
        if update is None:
            return
        pending.clear()
        pending.update(update)
        if cancel_timer is None:
            cancel_timer = async_call_later(hass, MQTT_DEBOUNCE_SECONDS, _fire)

    @callback
    def _on_message(msg: "ReceiveMessage") -> None:
        _queue(parse_event_message(msg.payload))

    @callback
    def _on_review_message(msg: "ReceiveMessage") -> None:
        _queue(parse_review_message(msg.payload))

    try:
        unsub_events = await mqtt.async_subscribe(hass, topic, _on_message)
    except HomeAssistantError as err:
        _LOGGER.warning("Could not subscribe to %s; live feed updates disabled: %s", topic, err)
        return _noop
    try:
        unsub_reviews = await mqtt.async_subscribe(hass, review_topic, _on_review_message)
    except HomeAssistantError as err:
        _LOGGER.warning("Could not subscribe to %s; new alerts appear when they end: %s", review_topic, err)
        unsub_reviews = _noop

    @callback
    def _unsubscribe() -> None:
        nonlocal cancel_timer
        if cancel_timer is not None:
            cancel_timer()
            cancel_timer = None
        pending.clear()
        unsub_events()
        unsub_reviews()

    return _unsubscribe
