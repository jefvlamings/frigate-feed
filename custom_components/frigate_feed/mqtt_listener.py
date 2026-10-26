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


def parse_event_message(payload: Any) -> dict[str, Any] | None:
    """Turn a raw `<prefix>/events` payload into a feed update, or None to ignore it."""
    if isinstance(payload, (bytes, bytearray)):
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError:
            return None
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
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


async def async_setup_mqtt_listener(
    hass: HomeAssistant, source: FrigateSource, manager: "FrigateFeedManager"
) -> Callable[[], None]:
    """Subscribe to f"{source.topic_prefix}/events" and return an unsubscribe callable.

    For `new` and `end` messages whose `after.label` maps onto a feed bucket:
    debounce MQTT_DEBOUNCE_SECONDS, then call manager.invalidate_cache() and
    async_dispatcher_send(hass, SIGNAL_FEED_UPDATED,
        {"type": ..., "event_id": ..., "camera": ..., "label": <bucket>}).
    `update` messages and unmapped labels are ignored. When MQTT is not set up,
    log a warning and return a no-op unsubscribe.
    """
    topic = f"{source.topic_prefix}/events"
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
        update = parse_event_message(msg.payload)
        if update is None:
            return
        pending.clear()
        pending.update(update)
        if cancel_timer is None:
            cancel_timer = async_call_later(hass, MQTT_DEBOUNCE_SECONDS, _fire)

    try:
        unsub_mqtt = await mqtt.async_subscribe(hass, topic, _on_message)
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
        unsub_mqtt()

    return _unsubscribe
