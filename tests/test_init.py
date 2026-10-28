"""End-to-end tests: real config entry setup, websocket API and signed media fetch."""

from __future__ import annotations

import math
from typing import Any

from pytest_homeassistant_custom_component.common import MockConfigEntry

from yarl import URL

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from custom_components.frigate_feed.const import (
    CONF_FRIGATE_ENTRY_ID,
    DOMAIN,
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
    # Items are review items: the package-only one is unmapped and the ~32h old one is
    # outside retention.
    assert ids == [
        "1793016151.200000-rev001",
        "1793015453.600000-rev002",
        "1793014800.300000-rev003",
        "1793012000.600000-rev004",
        "1793010001.000000-rev006",
    ]
    assert {item["label"] for item in items} == {"person", "pet", "vehicle"}
    assert [item["start_ts"] for item in items] == sorted(
        (item["start_ts"] for item in items), reverse=True
    )

    ended = next(item for item in items if item["id"] == "1793015453.600000-rev002")
    event_id = "1793015453.52062-q2c2mg"
    assert ended["recording"]["status"] == "linked"
    assert ended["camera_name"] == "Front Door"
    assert [detection["event_id"] for detection in ended["detections"]] == [event_id]
    snapshot_url = ended["snapshot_url"]
    clip_url = ended["recording"]["local_url"]
    assert snapshot_url.startswith(f"/api/frigate_feed/media/{event_id}/snapshot.jpg?v=")
    assert "authSig=" in snapshot_url
    assert clip_url.startswith(f"/api/frigate_feed/media/review/{ended['id']}/clip.mp4?authSig=")

    in_progress = next(item for item in items if item["id"] == "1793016151.200000-rev001")
    assert in_progress["recording"] == {"status": "pending"}

    # Signed URLs are reused, so browsers can serve the media from their cache.
    msg = await _ws(client, {"type": "frigate_feed/list"})
    again = next(item for item in msg["result"]["items"] if item["id"] == ended["id"])
    assert again["snapshot_url"] == snapshot_url
    assert again["thumbnail_url"] == ended["thumbnail_url"]

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
        f"{FRIGATE_URL}/api/events/{event_id}/snapshot.jpg",
        content=b"jpeg-bytes",
        headers={"Content-Type": "image/jpeg"},
    )
    http = await hass_client_no_auth()
    resp = await http.get(snapshot_url)
    assert resp.status == 200
    assert await resp.read() == b"jpeg-bytes"
    assert resp.headers["Content-Type"].startswith("image/jpeg")
    assert resp.headers["Cache-Control"] == "private, max-age=86400, immutable"
    unsigned = await http.get(snapshot_url.split("?", 1)[0])
    assert unsigned.status == 401

    # The object thumbnail is the same snapshot, cropped by Frigate; the signature covers
    # the crop parameters, which are forwarded upstream.
    thumbnail_url = ended["thumbnail_url"]
    assert thumbnail_url.startswith(f"/api/frigate_feed/media/{event_id}/snapshot.jpg?")
    assert {"crop": "1", "bbox": "0", "height": "400", "quality": "80"}.items() <= dict(
        URL(thumbnail_url).query
    ).items()
    resp = await http.get(thumbnail_url)
    assert resp.status == 200
    upstream_url = aioclient_mock.mock_calls[-1][1]
    assert dict(upstream_url.query) == {"crop": "1", "bbox": "0", "height": "400", "quality": "80"}
    tampered = thumbnail_url.replace("crop=1", "crop=0")
    assert (await http.get(tampered)).status == 401

    # The review clip is the camera recording over the review window plus pre/post capture.
    review = next(review for review in fake_frigate.reviews if review["id"] == ended["id"])
    clip_start = math.floor(review["start_time"] - 5)  # front_door alert pre_capture
    clip_end = math.ceil(review["end_time"] + 8)  # front_door alert post_capture
    aioclient_mock.get(
        f"{FRIGATE_URL}/api/front_door/start/{clip_start}/end/{clip_end}/clip.mp4",
        content=b"mp4-bytes",
        headers={"Content-Type": "video/mp4"},
    )
    resp = await http.get(clip_url)
    assert resp.status == 200
    assert await resp.read() == b"mp4-bytes"

    msg = await _ws(client, {"type": "frigate_feed/delete_item", "item_id": ended["id"]})
    assert msg["success"], msg
    msg = await _ws(client, {"type": "frigate_feed/list"})
    assert ended["id"] not in [item["id"] for item in msg["result"]["items"]]

    msg = await _ws(client, {"type": "frigate_feed/list", "labels": ["vehicle"]})
    assert {item["label"] for item in msg["result"]["items"]} == {"vehicle"}

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_list_reports_frigate_unavailable(
    hass: HomeAssistant, fake_frigate: FakeFrigate, hass_ws_client
) -> None:
    await _setup_feed(hass, fake_frigate)
    fake_frigate.client.fail = True
    client = await hass_ws_client(hass)
    msg = await _ws(client, {"type": "frigate_feed/list"})
    assert not msg["success"]
    assert msg["error"]["code"] == "frigate_unavailable"
