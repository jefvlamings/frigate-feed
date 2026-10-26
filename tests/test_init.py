"""End-to-end tests: real config entry setup, websocket API and signed media fetch."""

from __future__ import annotations

from typing import Any

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from custom_components.frigate_feed.const import (
    CONF_FRIGATE_ENTRY_ID,
    DOMAIN,
    SIGNAL_FEED_UPDATED,
)

from .conftest import FRIGATE_URL, FakeFrigate


async def _setup_feed(hass: HomeAssistant, fake_frigate: FakeFrigate) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Frigate Feed",
        unique_id=DOMAIN,
        data={CONF_FRIGATE_ENTRY_ID: fake_frigate.entry.entry_id},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


async def _ws(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    await client.send_json_auto_id(payload)
    return await client.receive_json()


async def test_setup_retries_without_frigate_runtime_data(
    hass: HomeAssistant, fake_frigate: FakeFrigate
) -> None:
    hass.data["frigate"].pop(fake_frigate.entry.entry_id)
    entry = MockConfigEntry(
        domain=DOMAIN, data={CONF_FRIGATE_ENTRY_ID: fake_frigate.entry.entry_id}
    )
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_list_resolve_delete_and_media(
    hass: HomeAssistant,
    fake_frigate: FakeFrigate,
    hass_ws_client,
    hass_client_no_auth,
    aioclient_mock,
) -> None:
    entry = await _setup_feed(hass, fake_frigate)
    client = await hass_ws_client(hass)

    msg = await _ws(client, {"type": "frigate_feed/list"})
    assert msg["success"], msg
    result = msg["result"]
    assert result["enabled_labels"] == ["person", "pet", "vehicle"]
    assert result["retention_hours"] == 24
    items = result["items"]
    ids = [item["id"] for item in items]
    # package is unmapped and the ~32h old event is outside retention.
    assert "1793012000.500000-pkg001" not in ids
    assert "1792900000.000000-old001" not in ids
    assert {item["label"] for item in items} == {"person", "pet", "vehicle"}
    assert [item["start_ts"] for item in items] == sorted(
        (item["start_ts"] for item in items), reverse=True
    )

    ended = next(item for item in items if item["id"] == "1793015453.52062-q2c2mg")
    assert ended["recording"]["status"] == "linked"
    assert ended["camera_name"] == "Front Door"
    snapshot_url = ended["snapshot_url"]
    clip_url = ended["recording"]["local_url"]
    assert snapshot_url.startswith(f"/api/frigate_feed/media/{ended['id']}/snapshot.jpg?authSig=")
    assert clip_url.startswith(f"/api/frigate_feed/media/{ended['id']}/clip.mp4?authSig=")

    in_progress = next(item for item in items if item["id"] == "1793016151.051489-lzerqk")
    assert in_progress["recording"] == {"status": "pending"}

    msg = await _ws(
        client, {"type": "frigate_feed/resolve_recording", "item_id": ended["id"]}
    )
    assert msg["success"], msg
    assert msg["result"]["status"] == "linked"
    assert "authSig=" in msg["result"]["local_url"]

    msg = await _ws(client, {"type": "frigate_feed/resolve_recording", "item_id": "1.0-nope"})
    assert not msg["success"]
    assert msg["error"]["code"] == "not_found"

    # Signed media URL works without an auth header and hits Frigate with its auth.
    aioclient_mock.get(
        f"{FRIGATE_URL}/api/events/{ended['id']}/snapshot.jpg",
        content=b"jpeg-bytes",
        headers={"Content-Type": "image/jpeg"},
    )
    http = await hass_client_no_auth()
    resp = await http.get(snapshot_url)
    assert resp.status == 200
    assert await resp.read() == b"jpeg-bytes"
    assert resp.headers["Content-Type"].startswith("image/jpeg")
    unsigned = await http.get(snapshot_url.split("?", 1)[0])
    assert unsigned.status == 401

    msg = await _ws(client, {"type": "frigate_feed/delete_item", "item_id": ended["id"]})
    assert msg["success"], msg
    msg = await _ws(client, {"type": "frigate_feed/list"})
    assert ended["id"] not in [item["id"] for item in msg["result"]["items"]]

    msg = await _ws(client, {"type": "frigate_feed/list", "labels": ["vehicle"]})
    assert {item["label"] for item in msg["result"]["items"]} == {"vehicle"}

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_subscribe_forwards_feed_updates(
    hass: HomeAssistant, fake_frigate: FakeFrigate, hass_ws_client
) -> None:
    await _setup_feed(hass, fake_frigate)
    client = await hass_ws_client(hass)

    msg = await _ws(client, {"type": "frigate_feed/subscribe"})
    assert msg["success"], msg
    sub_id = msg["id"]

    payload = {"type": "end", "event_id": "1.0-abc", "camera": "front_door", "label": "person"}
    async_dispatcher_send(hass, SIGNAL_FEED_UPDATED, payload)
    event = await client.receive_json()
    assert event["id"] == sub_id
    assert event["type"] == "event"
    assert event["event"] == {"event": payload}


async def test_list_reports_frigate_unavailable(
    hass: HomeAssistant, fake_frigate: FakeFrigate, hass_ws_client
) -> None:
    await _setup_feed(hass, fake_frigate)
    fake_frigate.client.fail = True
    client = await hass_ws_client(hass)
    msg = await _ws(client, {"type": "frigate_feed/list"})
    assert not msg["success"]
    assert msg["error"]["code"] == "frigate_unavailable"
