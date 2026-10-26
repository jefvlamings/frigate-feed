"""Authenticated media proxy for Frigate event snapshots and clips."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.core import HomeAssistant

from .frigate_source import FrigateSource


def async_register_media_views(
    hass: HomeAssistant, get_source: Callable[[], FrigateSource | None]
) -> None:
    """Register the media proxy view once (idempotent).

    The view is served at MEDIA_URL ("/api/frigate_feed/media/{event_id}/{kind}"),
    requires HA auth (signed paths work), validates event_id and kind (MEDIA_KINDS),
    and streams `{source.base_url}/api/events/{event_id}/{kind}` using
    `source.auth_headers()` and `source.verify_ssl`. Range/If-Range request headers
    and the upstream status are passed through. 404 when the source is unavailable or
    the event is unknown, 502 on upstream errors.
    """
    raise NotImplementedError


def sign_item_urls(hass: HomeAssistant, item: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of an item dict with snapshot_url and recording.local_url signed.

    Must be called while handling a websocket command so async_sign_path can bind
    the signature to the current connection's refresh token. Uses SIGNED_URL_EXPIRY.
    """
    raise NotImplementedError
