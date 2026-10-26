"""Adapter around the official Frigate integration's runtime data.

The official integration (domain `frigate`) stores per config entry:
    hass.data["frigate"][entry_id] = {"client": FrigateApiClient, "config": <api/config dict>, ...}

This module only reads that data through string keys and never imports
`custom_components.frigate`, so frigate_feed loads (and tests run) without it.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import DEFAULT_PRE_CAPTURE_SECONDS, FRIGATE_DOMAIN

_LOGGER = logging.getLogger(__name__)


class FrigateNotReady(Exception):
    """Raised when the Frigate integration entry is missing, not loaded, or unreachable."""


def frigate_entries(hass: HomeAssistant) -> list[ConfigEntry]:
    """Return all config entries of the official Frigate integration."""
    return list(hass.config_entries.async_entries(FRIGATE_DOMAIN))


def _runtime_data(hass: HomeAssistant, entry_id: str) -> dict[str, Any] | None:
    """Return hass.data["frigate"][entry_id] when it is a dict, else None."""
    domain_data = hass.data.get(FRIGATE_DOMAIN)
    if not isinstance(domain_data, dict):
        return None
    data = domain_data.get(entry_id)
    return data if isinstance(data, dict) else None


def _frigate_entry(hass: HomeAssistant, entry_id: str) -> ConfigEntry | None:
    """Return the Frigate config entry with this id, or None."""
    entry = hass.config_entries.async_get_entry(entry_id) if entry_id else None
    if entry is None or entry.domain != FRIGATE_DOMAIN:
        return None
    return entry


def async_get_frigate_source(hass: HomeAssistant, frigate_entry_id: str) -> "FrigateSource":
    """Return a FrigateSource for a loaded Frigate config entry.

    Raises FrigateNotReady when the entry does not exist or its runtime data
    (client + config) is not available yet.
    """
    if _frigate_entry(hass, frigate_entry_id) is None:
        raise FrigateNotReady(f"Frigate config entry {frigate_entry_id!r} not found")
    data = _runtime_data(hass, frigate_entry_id)
    if data is None or data.get("client") is None or not isinstance(data.get("config"), dict):
        raise FrigateNotReady(f"Frigate config entry {frigate_entry_id!r} is not loaded")
    return FrigateSource(hass, frigate_entry_id)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


class FrigateSource:
    """Read-only view on one Frigate integration config entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self.hass = hass
        self.entry_id = entry_id

    @property
    def client(self) -> Any:
        """The integration's FrigateApiClient (hass.data["frigate"][entry_id]["client"])."""
        data = _runtime_data(self.hass, self.entry_id)
        client = data.get("client") if data else None
        if client is None:
            raise FrigateNotReady(f"Frigate config entry {self.entry_id!r} has no client")
        return client

    @property
    def config(self) -> dict[str, Any]:
        """Cached Frigate /api/config (hass.data["frigate"][entry_id]["config"])."""
        data = _runtime_data(self.hass, self.entry_id)
        return _as_dict(data.get("config") if data else None)

    def _entry_data(self) -> dict[str, Any]:
        entry = _frigate_entry(self.hass, self.entry_id)
        if entry is None:
            raise FrigateNotReady(f"Frigate config entry {self.entry_id!r} not found")
        return dict(entry.data)

    @property
    def base_url(self) -> str:
        """Frigate base URL from the Frigate entry data (`url`), without trailing slash."""
        return str(self._entry_data().get("url") or "").rstrip("/")

    @property
    def verify_ssl(self) -> bool:
        """Frigate entry `validate_ssl` (default True)."""
        return bool(self._entry_data().get("validate_ssl", True))

    @property
    def topic_prefix(self) -> str:
        """MQTT topic prefix from config["mqtt"]["topic_prefix"] (default "frigate")."""
        prefix = _as_dict(self.config.get("mqtt")).get("topic_prefix")
        return str(prefix) if prefix else "frigate"

    def auth_headers(self) -> dict[str, str]:
        """Headers that authenticate a request to Frigate (client.get_auth_headers())."""
        try:
            getter = getattr(self.client, "get_auth_headers", None)
            headers = getter() if callable(getter) else None
        except Exception:  # noqa: BLE001 - the client is third-party code
            _LOGGER.debug("Frigate client get_auth_headers failed", exc_info=True)
            return {}
        if not isinstance(headers, dict):
            return {}
        return {str(key): str(value) for key, value in headers.items()}

    def camera_names(self) -> list[str]:
        """Frigate camera names from the config, sorted."""
        return sorted(_as_dict(self.config.get("cameras")))

    def camera_display_name(self, camera: str) -> str:
        """Human camera name: underscores -> spaces, title-cased ("tuin_huis" -> "Tuin Huis")."""
        return " ".join(str(camera).replace("_", " ").split()).title()

    def pre_capture_seconds(self, camera: str, severity: str | None = None) -> float:
        """Recording pre-capture seconds for a camera.

        Uses cameras[camera].record.alerts.pre_capture for severity "alert",
        record.detections.pre_capture otherwise, falling back to DEFAULT_PRE_CAPTURE_SECONDS.
        """
        return self._capture_seconds(camera, severity, "pre_capture")

    def post_capture_seconds(self, camera: str, severity: str | None = None) -> float:
        """Recording post-capture seconds for a camera (same lookup as pre_capture_seconds)."""
        return self._capture_seconds(camera, severity, "post_capture")

    def _capture_seconds(self, camera: str, severity: str | None, field: str) -> float:
        cameras = _as_dict(self.config.get("cameras"))
        record = _as_dict(_as_dict(cameras.get(camera)).get("record"))
        order = ("alerts", "detections") if severity == "alert" else ("detections", "alerts")
        for key in order:
            value = _as_dict(record.get(key)).get(field)
            if isinstance(value, bool):
                continue
            try:
                seconds = float(value)
            except (TypeError, ValueError):
                continue
            if seconds >= 0:
                return seconds
        return float(DEFAULT_PRE_CAPTURE_SECONDS)

    def source_entity_id(self, camera: str, frigate_label: str) -> str | None:
        """Entity id for history/logbook links.

        Looks up the entity registry for unique_id
        f"{entry_id}:occupancy_sensor:{camera}_{frigate_label}", then
        f"{entry_id}:camera:{camera}". Returns None when neither exists.
        """
        registry = er.async_get(self.hass)
        candidates = (
            ("binary_sensor", f"{self.entry_id}:occupancy_sensor:{camera}_{frigate_label}"),
            ("camera", f"{self.entry_id}:camera:{camera}"),
        )
        for domain, unique_id in candidates:
            entity_id = registry.async_get_entity_id(domain, FRIGATE_DOMAIN, unique_id)
            if entity_id:
                return entity_id
        return None

    async def async_get_events(
        self, *, after: float, limit: int, labels: list[str] | None
    ) -> list[dict[str, Any]]:
        """Return Frigate events (newest first) started after `after` (epoch seconds).

        Raises FrigateNotReady on client errors.
        """
        client = self.client
        try:
            events = await client.async_get_events(after=int(after), limit=limit, labels=labels)
        except Exception as err:  # noqa: BLE001 - client raises its own error types
            raise FrigateNotReady(f"Failed to fetch Frigate events: {err}") from err
        if not isinstance(events, list):
            return []
        result = [event for event in events if isinstance(event, dict)]
        result.sort(key=_event_start_time, reverse=True)
        return result

    async def async_get_reviews(
        self, *, after: float, limit: int, labels: list[str] | None
    ) -> list[dict[str, Any]]:
        """Return Frigate review items (newest first) started after `after` (epoch seconds).

        Raises FrigateNotReady on client errors.
        """
        client = self.client
        try:
            reviews = await client.async_get_reviews(after=int(after), limit=limit, labels=labels)
        except Exception as err:  # noqa: BLE001 - client raises its own error types
            raise FrigateNotReady(f"Failed to fetch Frigate review items: {err}") from err
        if not isinstance(reviews, list):
            return []
        result = [review for review in reviews if isinstance(review, dict)]
        result.sort(key=_event_start_time, reverse=True)
        return result

    async def async_get_review(self, review_id: str) -> dict[str, Any] | None:
        """Return one Frigate review item, or None when it does not exist or the call fails.

        The client has no single-review helper (and `async_get_path` appends a slash that
        Frigate redirects), so this calls GET /api/review/{id} through `api_wrapper`.
        """
        try:
            review = await self.client.api_wrapper("get", f"{self.base_url}/api/review/{review_id}")
        except Exception:  # noqa: BLE001 - not found and transport errors look alike
            _LOGGER.debug("Failed to fetch Frigate review item %s", review_id, exc_info=True)
            return None
        return review if isinstance(review, dict) and review.get("id") == review_id else None

    def recording_enabled(self, camera: str) -> bool:
        """True unless cameras[camera].record.enabled is explicitly false."""
        cameras = _as_dict(self.config.get("cameras"))
        record = _as_dict(_as_dict(cameras.get(camera)).get("record"))
        return record.get("enabled") is not False

    async def async_get_event(self, event_id: str) -> dict[str, Any] | None:
        """Return one Frigate event, or None when it does not exist or the call fails."""
        try:
            event = await self.client.async_get_event(event_id)
        except Exception:  # noqa: BLE001 - not found and transport errors look alike
            _LOGGER.debug("Failed to fetch Frigate event %s", event_id, exc_info=True)
            return None
        return event if isinstance(event, dict) else None


def _event_start_time(event: dict[str, Any]) -> float:
    try:
        return float(event.get("start_time") or 0)
    except (TypeError, ValueError):
        return 0.0
