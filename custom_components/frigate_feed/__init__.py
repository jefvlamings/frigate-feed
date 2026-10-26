"""Frigate feed integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
import logging
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.components.lovelace.const import (
    CONF_RESOURCE_TYPE_WS,
    CONF_URL,
    LOVELACE_DATA,
)
from homeassistant.components.lovelace.resources import ResourceStorageCollection
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_ID, EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import (
    CARD_FILENAME,
    CARD_URL_PATH,
    CONF_ENABLED_LABELS,
    CONF_FRIGATE_ENTRY_ID,
    CONF_MAX_DETECTIONS,
    CONF_RETENTION_HOURS,
    DEFAULT_ENABLED_DETECTION_LABELS,
    DEFAULT_MAX_DETECTIONS,
    DEFAULT_RETENTION_HOURS,
    DOMAIN,
    MAX_MAX_DETECTIONS,
    MAX_RETENTION_HOURS,
    MIN_MAX_DETECTIONS,
    MIN_RETENTION_HOURS,
    SIGNAL_FEED_UPDATED,
    SUPPORTED_DETECTION_LABELS,
)
from .feed import FrigateFeedManager
from .frigate_source import FrigateNotReady, FrigateSource, async_get_frigate_source
from .media import async_register_media_views, sign_item_urls
from .mqtt_listener import async_setup_mqtt_listener
from .storage import HiddenStore

_LOGGER = logging.getLogger(__name__)
_LOCAL_CARD_URL_PATH = "/local/frigate-feed-card.js"
_INTEGRATION_VERSION_CACHE_KEY = f"{DOMAIN}_integration_version"


@dataclass(slots=True)
class FrigateFeedData:
    """Runtime data stored on the config entry."""

    source: FrigateSource
    manager: FrigateFeedManager
    mqtt_unsub: Callable[[], None] | None = None
    options_unsub: Callable[[], None] | None = None


FrigateFeedConfigEntry = ConfigEntry[FrigateFeedData]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up integration from YAML (none)."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: FrigateFeedConfigEntry) -> bool:
    """Set up Frigate feed from a config entry."""
    frigate_entry_id = str(entry.data.get(CONF_FRIGATE_ENTRY_ID, ""))
    try:
        source = async_get_frigate_source(hass, frigate_entry_id)
    except FrigateNotReady as err:
        raise ConfigEntryNotReady(f"Frigate integration entry not ready: {err}") from err

    manager = FrigateFeedManager(
        hass,
        source,
        HiddenStore(hass),
        _enabled_labels_from_entry(entry),
        _retention_hours_from_entry(entry),
        _max_detections_from_entry(entry),
    )
    await manager.async_start()
    mqtt_unsub = await async_setup_mqtt_listener(hass, source, manager)
    options_unsub = entry.add_update_listener(_async_reload_entry_on_update)
    entry.runtime_data = FrigateFeedData(
        source=source,
        manager=manager,
        mqtt_unsub=mqtt_unsub,
        options_unsub=options_unsub,
    )
    async_register_media_views(hass, lambda: _current_source(hass))
    await _async_register_card_resource(hass)
    await _async_ensure_lovelace_card_resource(hass)
    _async_register_ws_commands(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: FrigateFeedConfigEntry) -> bool:
    """Unload the config entry."""
    data = entry.runtime_data
    if data.options_unsub:
        data.options_unsub()
    if data.mqtt_unsub:
        data.mqtt_unsub()
    await data.manager.async_stop()
    return True


async def _async_reload_entry_on_update(hass: HomeAssistant, entry: FrigateFeedConfigEntry) -> None:
    """Reload config entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


def _loaded_entry(hass: HomeAssistant) -> FrigateFeedConfigEntry | None:
    for entry in hass.config_entries.async_entries(DOMAIN):
        if entry.state is ConfigEntryState.LOADED and getattr(entry, "runtime_data", None):
            return entry
    return None


def _current_source(hass: HomeAssistant) -> FrigateSource | None:
    entry = _loaded_entry(hass)
    return entry.runtime_data.source if entry else None


async def _async_register_card_resource(hass: HomeAssistant) -> None:
    """Expose the bundled Lovelace card JavaScript as a static URL."""
    if hass.data.get(f"{DOMAIN}_card_registered"):
        return

    card_file = Path(__file__).parent / "frontend" / CARD_FILENAME
    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARD_URL_PATH, str(card_file), cache_headers=False)]
    )
    hass.data[f"{DOMAIN}_card_registered"] = True


async def _async_ensure_lovelace_card_resource(hass: HomeAssistant) -> None:
    """Ensure Lovelace storage resources include the bundled card."""
    if not await _async_try_add_lovelace_card_resource(hass):
        @callback
        def _on_started(_event) -> None:
            hass.async_create_task(_async_try_add_lovelace_card_resource(hass))

        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _on_started)


async def _async_try_add_lovelace_card_resource(hass: HomeAssistant) -> bool:
    """Add the card resource in Lovelace storage mode if needed."""
    lovelace_data = hass.data.get(LOVELACE_DATA)
    if lovelace_data is None:
        return False

    resources = lovelace_data.resources
    if not isinstance(resources, ResourceStorageCollection):
        _LOGGER.debug("Lovelace is not in storage resource mode; skipping card resource auto-add")
        return True

    await resources.async_get_info()
    integration_version = await _async_integration_version(hass)
    target_url = _card_resource_url(integration_version)
    existing_card_item: dict | None = None
    for item in resources.async_items() or []:
        url = str(item.get(CONF_URL, ""))
        base_url = url.split("?", 1)[0]
        if base_url == _LOCAL_CARD_URL_PATH:
            _LOGGER.debug("Local dev card resource detected; skipping auto-add for %s", CARD_URL_PATH)
            return True
        if base_url == CARD_URL_PATH:
            existing_card_item = item
            break

    if existing_card_item is None:
        await resources.async_create_item(
            {CONF_RESOURCE_TYPE_WS: "module", CONF_URL: target_url}
        )
        _LOGGER.info("Added Lovelace resource for Frigate Feed card: %s", target_url)
        return True

    if _resource_version_from_url(str(existing_card_item.get(CONF_URL, ""))) == integration_version:
        return True

    item_id = existing_card_item.get(CONF_ID)
    if not isinstance(item_id, str) or not item_id:
        return True
    await resources.async_update_item(item_id, {CONF_URL: target_url})
    _LOGGER.info("Updated Lovelace resource for Frigate Feed card: %s", target_url)
    return True


def _integration_version() -> str:
    manifest_path = Path(__file__).parent / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return "dev"
    version = manifest.get("version")
    return str(version) if version else "dev"


async def _async_integration_version(hass: HomeAssistant) -> str:
    if cached := hass.data.get(_INTEGRATION_VERSION_CACHE_KEY):
        return str(cached)
    version = await hass.async_add_executor_job(_integration_version)
    hass.data[_INTEGRATION_VERSION_CACHE_KEY] = version
    return version


def _card_resource_url(version: str) -> str:
    return f"{CARD_URL_PATH}?v={version}"


def _resource_version_from_url(url: str) -> str | None:
    query = parse_qs(urlsplit(url).query)
    value = query.get("v")
    if not value:
        return None
    return value[0]


@callback
def _async_register_ws_commands(hass: HomeAssistant) -> None:
    if hass.data.get(f"{DOMAIN}_ws_registered"):
        return
    websocket_api.async_register_command(hass, ws_list_items)
    websocket_api.async_register_command(hass, ws_resolve_recording)
    websocket_api.async_register_command(hass, ws_delete_item)
    websocket_api.async_register_command(hass, ws_subscribe)
    hass.data[f"{DOMAIN}_ws_registered"] = True


@websocket_api.websocket_command(
    {
        "type": "frigate_feed/list",
        vol.Optional("labels"): [cv.string],
    }
)
@websocket_api.async_response
async def ws_list_items(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Return timeline items newest-first."""
    entry = _loaded_entry(hass)
    if entry is None:
        connection.send_error(msg["id"], "not_loaded", "frigate_feed is not loaded")
        return

    manager = entry.runtime_data.manager
    requested = {
        str(label).strip().lower()
        for label in (msg.get("labels") or [])
        if str(label).strip().lower() in SUPPORTED_DETECTION_LABELS
    }
    try:
        items = await manager.async_list_items(requested or None)
    except FrigateNotReady as err:
        connection.send_error(msg["id"], "frigate_unavailable", str(err))
        return

    connection.send_result(
        msg["id"],
        {
            "items": [sign_item_urls(hass, item.as_dict()) for item in items],
            "enabled_labels": sorted(manager.get_enabled_labels()),
            "retention_hours": manager.get_retention_hours(),
        },
    )


@websocket_api.websocket_command(
    {
        "type": "frigate_feed/resolve_recording",
        vol.Required("item_id"): cv.string,
        vol.Optional("final_attempt", default=False): cv.boolean,
    }
)
@websocket_api.async_response
async def ws_resolve_recording(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Re-check the recording for one item."""
    entry = _loaded_entry(hass)
    if entry is None:
        connection.send_error(msg["id"], "not_loaded", "frigate_feed is not loaded")
        return

    try:
        recording = await entry.runtime_data.manager.async_resolve_recording(
            msg["item_id"], final_attempt=msg["final_attempt"]
        )
    except ValueError:
        connection.send_error(msg["id"], "not_found", "item id not found")
        return
    except FrigateNotReady as err:
        connection.send_error(msg["id"], "frigate_unavailable", str(err))
        return

    signed = sign_item_urls(hass, {"snapshot_url": None, "recording": recording})
    connection.send_result(msg["id"], signed["recording"])


@websocket_api.websocket_command(
    {
        "type": "frigate_feed/delete_item",
        vol.Required("item_id"): cv.string,
    }
)
@websocket_api.async_response
async def ws_delete_item(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Hide one feed item (Frigate data is never modified)."""
    entry = _loaded_entry(hass)
    if entry is None:
        connection.send_error(msg["id"], "not_loaded", "frigate_feed is not loaded")
        return

    try:
        await entry.runtime_data.manager.async_hide_item(msg["item_id"])
    except ValueError:
        connection.send_error(msg["id"], "not_found", "item id not found")
        return

    connection.send_result(msg["id"], {"ok": True})


@websocket_api.websocket_command({"type": "frigate_feed/subscribe"})
@callback
def ws_subscribe(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict
) -> None:
    """Push a message whenever the feed changes (debounced MQTT updates)."""

    @callback
    def _forward(payload: dict) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], {"event": payload}))

    connection.subscriptions[msg["id"]] = async_dispatcher_connect(
        hass, SIGNAL_FEED_UPDATED, _forward
    )
    connection.send_result(msg["id"])


def _enabled_labels_from_entry(entry: FrigateFeedConfigEntry) -> set[str]:
    raw = entry.options.get(CONF_ENABLED_LABELS)
    selected = [str(value).lower() for value in raw] if isinstance(raw, list) else list(
        DEFAULT_ENABLED_DETECTION_LABELS
    )
    normalized = {value for value in selected if value in SUPPORTED_DETECTION_LABELS}
    return normalized or set(DEFAULT_ENABLED_DETECTION_LABELS)


def _retention_hours_from_entry(entry: FrigateFeedConfigEntry) -> int:
    raw = entry.options.get(CONF_RETENTION_HOURS, DEFAULT_RETENTION_HOURS)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_RETENTION_HOURS
    return max(MIN_RETENTION_HOURS, min(MAX_RETENTION_HOURS, value))


def _max_detections_from_entry(entry: FrigateFeedConfigEntry) -> int:
    raw = entry.options.get(CONF_MAX_DETECTIONS, DEFAULT_MAX_DETECTIONS)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_MAX_DETECTIONS
    return max(MIN_MAX_DETECTIONS, min(MAX_MAX_DETECTIONS, value))
