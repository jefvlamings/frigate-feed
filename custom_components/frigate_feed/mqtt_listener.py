"""MQTT listener that pushes live feed updates."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant

from .frigate_source import FrigateSource

if TYPE_CHECKING:
    from .feed import FrigateFeedManager


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
    raise NotImplementedError
