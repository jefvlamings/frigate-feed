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
LIVE_REVIEW_TYPES: frozenset[str] = frozenset({"new", "update", "end"})


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


def _review_list(review: Any, key: str) -> list[str]:
    data = review.get("data") if isinstance(review, dict) else None
    values = data.get(key) if isinstance(data, dict) else None
    return [str(value) for value in values] if isinstance(values, list) else []


def parse_review_message(payload: Any) -> dict[str, Any] | None:
    """Turn a raw `<prefix>/reviews` payload into a feed update, or None to ignore it.

    Each feed item is a Frigate review item. It changes when Frigate announces it
    (`new`), adds a detection or object to it (`update`), or closes it (`end`);
    other updates (e.g. a new thumbnail) are ignored, as are review items without a
    label that maps onto a feed bucket.
    """
    data = _decode_payload(payload)
    if data is None:
        return None
    review_type = data.get("type")
    after = data.get("after")
    if review_type not in LIVE_REVIEW_TYPES or not isinstance(after, dict):
        return None
    if review_type == "update":
        before = data.get("before")
        changed = any(
            set(_review_list(after, key)) - set(_review_list(before, key)) for key in ("detections", "objects")
        )
        if not changed:
            return None
    buckets = [bucket_frigate_label(obj) for obj in _review_list(after, "objects")]
    bucket = next((bucket for bucket in buckets if bucket), None)
    if bucket is None:
        return None
    return {
        "type": review_type,
        "review_id": after.get("id"),
        "camera": after.get("camera"),
        "label": bucket,
    }


async def async_setup_mqtt_listener(
    hass: HomeAssistant, source: FrigateSource, manager: "FrigateFeedManager"
) -> Callable[[], None]:
    """Subscribe to f"{source.topic_prefix}/reviews".

    Returns an unsubscribe callable. For review messages that change a feed item
    (see parse_review_message): debounce MQTT_DEBOUNCE_SECONDS, then call
    manager.invalidate_cache() and async_dispatcher_send(hass, SIGNAL_FEED_UPDATED,
        {"type": ..., "review_id": ..., "camera": ..., "label": <bucket>}).
    When MQTT is not set up, log a warning and return a no-op unsubscribe.
    """
    topic = f"{source.topic_prefix}/reviews"
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
    def _on_message(msg: "ReceiveMessage") -> None:
        nonlocal cancel_timer
        update = parse_review_message(msg.payload)
        if update is None:
            return
        pending.clear()
        pending.update(update)
        if cancel_timer is None:
            cancel_timer = async_call_later(hass, MQTT_DEBOUNCE_SECONDS, _fire)

    try:
        unsub = await mqtt.async_subscribe(hass, topic, _on_message)
    except HomeAssistantError as err:
        _LOGGER.warning("Could not subscribe to %s; live feed updates disabled: %s", topic, err)
        return _noop

    @callback
    def _unsubscribe() -> None:
        nonlocal cancel_timer
        if cancel_timer is not None:
            cancel_timer()
            cancel_timer = None
        pending.clear()
        unsub()

    return _unsubscribe
