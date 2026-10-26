"""Adapter around the official Frigate integration's runtime data.

The official integration (domain `frigate`) stores per config entry:
    hass.data["frigate"][entry_id] = {"client": FrigateApiClient, "config": <api/config dict>, ...}

This module only reads that data through string keys and never imports
`custom_components.frigate`, so frigate_feed loads (and tests run) without it.
"""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import FRIGATE_DOMAIN


class FrigateNotReady(Exception):
    """Raised when the Frigate integration entry is missing, not loaded, or unreachable."""


def frigate_entries(hass: HomeAssistant) -> list[ConfigEntry]:
    """Return all config entries of the official Frigate integration."""
    return list(hass.config_entries.async_entries(FRIGATE_DOMAIN))


def async_get_frigate_source(hass: HomeAssistant, frigate_entry_id: str) -> "FrigateSource":
    """Return a FrigateSource for a loaded Frigate config entry.

    Raises FrigateNotReady when the entry does not exist or its runtime data
    (client + config) is not available yet.
    """
    raise NotImplementedError


class FrigateSource:
    """Read-only view on one Frigate integration config entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self.hass = hass
        self.entry_id = entry_id

    @property
    def client(self) -> Any:
        """The integration's FrigateApiClient (hass.data["frigate"][entry_id]["client"])."""
        raise NotImplementedError

    @property
    def config(self) -> dict[str, Any]:
        """Cached Frigate /api/config (hass.data["frigate"][entry_id]["config"])."""
        raise NotImplementedError

    @property
    def base_url(self) -> str:
        """Frigate base URL from the Frigate entry data (`url`), without trailing slash."""
        raise NotImplementedError

    @property
    def verify_ssl(self) -> bool:
        """Frigate entry `validate_ssl` (default True)."""
        raise NotImplementedError

    @property
    def topic_prefix(self) -> str:
        """MQTT topic prefix from config["mqtt"]["topic_prefix"] (default "frigate")."""
        raise NotImplementedError

    def auth_headers(self) -> dict[str, str]:
        """Headers that authenticate a request to Frigate (client.get_auth_headers())."""
        raise NotImplementedError

    def camera_names(self) -> list[str]:
        """Frigate camera names from the config, sorted."""
        raise NotImplementedError

    def camera_display_name(self, camera: str) -> str:
        """Human camera name: underscores -> spaces, title-cased ("tuin_huis" -> "Tuin Huis")."""
        raise NotImplementedError

    def pre_capture_seconds(self, camera: str, severity: str | None = None) -> float:
        """Recording pre-capture seconds for a camera.

        Uses cameras[camera].record.alerts.pre_capture for severity "alert",
        record.detections.pre_capture otherwise, falling back to DEFAULT_PRE_CAPTURE_SECONDS.
        """
        raise NotImplementedError

    def source_entity_id(self, camera: str, frigate_label: str) -> str | None:
        """Entity id for history/logbook links.

        Looks up the entity registry for unique_id
        f"{entry_id}:occupancy_sensor:{camera}_{frigate_label}", then
        f"{entry_id}:camera:{camera}". Returns None when neither exists.
        """
        raise NotImplementedError

    async def async_get_events(
        self, *, after: float, limit: int, labels: list[str] | None
    ) -> list[dict[str, Any]]:
        """Return Frigate events (newest first) started after `after` (epoch seconds).

        Raises FrigateNotReady on client errors.
        """
        raise NotImplementedError

    async def async_get_event(self, event_id: str) -> dict[str, Any] | None:
        """Return one Frigate event, or None when it does not exist or the call fails."""
        raise NotImplementedError
