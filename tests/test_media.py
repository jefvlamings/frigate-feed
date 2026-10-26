"""Tests for the Frigate media proxy and URL signing."""

from __future__ import annotations

from datetime import timedelta
from http import HTTPStatus
import time
from typing import Any
from unittest.mock import patch

import aiohttp
import jwt
import pytest
from pytest_homeassistant_custom_component.common import async_fire_time_changed
import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util

from custom_components.frigate_feed import media
from custom_components.frigate_feed.media import (
    async_register_media_views,
    is_valid_event_id,
    sign_item_urls,
)

BASE_URL = "http://frigate.test:5000"
EVENT_ID = "1793016151.051489-lzerqk"
SNAPSHOT_PATH = f"/api/frigate_feed/media/{EVENT_ID}/snapshot.jpg"
CLIP_PATH = f"/api/frigate_feed/media/{EVENT_ID}/clip.mp4"
UPSTREAM_SNAPSHOT = f"{BASE_URL}/api/events/{EVENT_ID}/snapshot.jpg"
UPSTREAM_CLIP = f"{BASE_URL}/api/events/{EVENT_ID}/clip.mp4"
CLIP_BYTES = bytes(range(256)) * 4


class FakeSource:
    """Implements the parts of the FrigateSource contract used by media.py."""

    def __init__(self, base_url: str = BASE_URL, verify_ssl: bool = True) -> None:
        self.base_url = base_url
        self.verify_ssl = verify_ssl

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer frigate-token"}


class SourceHolder:
    def __init__(self) -> None:
        self.source: FakeSource | None = FakeSource()

    def __call__(self) -> FakeSource | None:
        return self.source


@pytest.fixture
async def holder(hass: HomeAssistant) -> SourceHolder:
    assert await async_setup_component(hass, "http", {})
    holder = SourceHolder()
    async_register_media_views(hass, holder)
    return holder


def _sent_headers(aioclient_mock, index: int = -1) -> dict[str, str]:
    return aioclient_mock.mock_calls[index][3]


def test_event_id_validation() -> None:
    assert is_valid_event_id(EVENT_ID)
    assert is_valid_event_id("1793016151.051489-LZERQK")
    for bad in ("", "abc", "1.2", "1.2-", "1.2-ab/cd", "../1.2-ab", "1.2-ab cd", "1.2-ab%2f"):
        assert not is_valid_event_id(bad), bad


async def test_register_is_idempotent(hass: HomeAssistant, holder: SourceHolder) -> None:
    routes_before = len(hass.http.app.router.routes())
    async_register_media_views(hass, holder)
    assert len(hass.http.app.router.routes()) == routes_before


async def test_requires_auth(hass: HomeAssistant, holder: SourceHolder, hass_client_no_auth, aioclient_mock) -> None:
    client = await hass_client_no_auth()
    resp = await client.get(SNAPSHOT_PATH)
    assert resp.status == HTTPStatus.UNAUTHORIZED
    assert aioclient_mock.call_count == 0


@pytest.mark.parametrize(
    "path",
    [
        f"/api/frigate_feed/media/{EVENT_ID}/preview.gif",
        f"/api/frigate_feed/media/{EVENT_ID}/thumbnail.jpg",
        "/api/frigate_feed/media/not-an-id/snapshot.jpg",
        "/api/frigate_feed/media/1.2-a_b/clip.mp4",
    ],
)
async def test_invalid_kind_or_id(hass: HomeAssistant, holder: SourceHolder, hass_client, aioclient_mock, path: str) -> None:
    client = await hass_client()
    resp = await client.get(path)
    assert resp.status == HTTPStatus.NOT_FOUND
    assert aioclient_mock.call_count == 0


async def test_source_unavailable(hass: HomeAssistant, holder: SourceHolder, hass_client, aioclient_mock) -> None:
    holder.source = None
    client = await hass_client()
    resp = await client.get(SNAPSHOT_PATH)
    assert resp.status == HTTPStatus.NOT_FOUND
    assert aioclient_mock.call_count == 0


async def test_snapshot_proxy_copies_headers(hass: HomeAssistant, holder: SourceHolder, hass_client, aioclient_mock) -> None:
    aioclient_mock.get(
        UPSTREAM_SNAPSHOT,
        content=b"jpegdata",
        headers={
            "Content-Type": "image/jpeg",
            "Content-Length": "8",
            "Cache-Control": "private, max-age=31536000",
            "ETag": '"abc"',
            "Last-Modified": "Mon, 26 Oct 2026 10:00:00 GMT",
            "X-Frame-Time": "1793016151.0",
            "Set-Cookie": "secret=1",
        },
    )
    client = await hass_client()
    resp = await client.get(f"{SNAPSHOT_PATH}?height=200&quality=70&evil=1&bbox=1")
    assert resp.status == HTTPStatus.OK
    assert await resp.read() == b"jpegdata"
    assert resp.headers["Content-Type"] == "image/jpeg"
    assert resp.headers["Content-Length"] == "8"
    assert resp.headers["Cache-Control"] == "private, max-age=31536000"
    assert resp.headers["ETag"] == '"abc"'
    assert resp.headers["Last-Modified"] == "Mon, 26 Oct 2026 10:00:00 GMT"
    assert "X-Frame-Time" not in resp.headers
    assert "Set-Cookie" not in resp.headers

    _, url, _, headers = aioclient_mock.mock_calls[-1]
    assert dict(url.query) == {"height": "200", "quality": "70"}
    assert headers["Authorization"] == "Bearer frigate-token"
    assert "Range" not in headers


async def test_clip_query_params_not_forwarded(hass: HomeAssistant, holder: SourceHolder, hass_client, aioclient_mock) -> None:
    aioclient_mock.get(UPSTREAM_CLIP, content=CLIP_BYTES, headers={"Content-Type": "video/mp4"})
    client = await hass_client()
    resp = await client.get(f"{CLIP_PATH}?height=200")
    assert resp.status == HTTPStatus.OK
    assert await resp.read() == CLIP_BYTES
    assert not aioclient_mock.mock_calls[-1][1].query


async def test_upstream_404(hass: HomeAssistant, holder: SourceHolder, hass_client, aioclient_mock) -> None:
    aioclient_mock.get(UPSTREAM_SNAPSHOT, status=HTTPStatus.NOT_FOUND, json={"success": False})
    client = await hass_client()
    resp = await client.get(SNAPSHOT_PATH)
    assert resp.status == HTTPStatus.NOT_FOUND


@pytest.mark.parametrize(
    "mock_kwargs",
    [
        {"status": HTTPStatus.INTERNAL_SERVER_ERROR},
        {"status": HTTPStatus.UNAUTHORIZED},
        {"exc": aiohttp.ClientConnectionError("boom")},
        {"exc": TimeoutError()},
    ],
)
@pytest.mark.parametrize("path, upstream", [(SNAPSHOT_PATH, UPSTREAM_SNAPSHOT), (CLIP_PATH, UPSTREAM_CLIP)])
async def test_upstream_errors_map_to_502(
    hass: HomeAssistant,
    holder: SourceHolder,
    hass_client,
    aioclient_mock,
    mock_kwargs: dict[str, Any],
    path: str,
    upstream: str,
) -> None:
    aioclient_mock.get(upstream, **mock_kwargs)
    client = await hass_client()
    for headers in ({}, {"Range": "bytes=0-"}):
        resp = await client.get(path, headers=headers)
        assert resp.status == HTTPStatus.BAD_GATEWAY


async def test_range_passthrough_when_upstream_supports_it(
    hass: HomeAssistant, holder: SourceHolder, hass_client, aioclient_mock
) -> None:
    aioclient_mock.get(
        UPSTREAM_CLIP,
        status=HTTPStatus.PARTIAL_CONTENT,
        content=CLIP_BYTES[:16],
        headers={
            "Content-Type": "video/mp4",
            "Content-Length": "16",
            "Content-Range": f"bytes 0-15/{len(CLIP_BYTES)}",
            "Accept-Ranges": "bytes",
        },
    )
    client = await hass_client()
    resp = await client.get(CLIP_PATH, headers={"Range": "bytes=0-15", "If-Range": '"v1"'})
    assert resp.status == HTTPStatus.PARTIAL_CONTENT
    assert await resp.read() == CLIP_BYTES[:16]
    assert resp.headers["Content-Range"] == f"bytes 0-15/{len(CLIP_BYTES)}"
    assert resp.headers["Accept-Ranges"] == "bytes"
    assert resp.headers["Content-Length"] == "16"
    sent = _sent_headers(aioclient_mock)
    assert sent["Range"] == "bytes=0-15"
    assert sent["If-Range"] == '"v1"'
    assert sent["Authorization"] == "Bearer frigate-token"

    # Without If-Range the cache path is used, but a 206 upstream is still passed through.
    resp = await client.get(CLIP_PATH, headers={"Range": "bytes=0-15"})
    assert resp.status == HTTPStatus.PARTIAL_CONTENT
    assert await resp.read() == CLIP_BYTES[:16]
    assert _sent_headers(aioclient_mock)["Range"] == "bytes=0-15"


async def test_range_served_from_buffer_when_upstream_ignores_it(
    hass: HomeAssistant, holder: SourceHolder, hass_client, aioclient_mock
) -> None:
    """Frigate answers clip.mp4 with a chunked 200 and no range support."""
    aioclient_mock.get(UPSTREAM_CLIP, content=CLIP_BYTES, headers={"Content-Type": "video/mp4"})
    client = await hass_client()
    size = len(CLIP_BYTES)

    resp = await client.get(CLIP_PATH, headers={"Range": "bytes=0-1"})
    assert resp.status == HTTPStatus.PARTIAL_CONTENT
    assert await resp.read() == CLIP_BYTES[:2]
    assert resp.headers["Content-Range"] == f"bytes 0-1/{size}"
    assert resp.headers["Accept-Ranges"] == "bytes"
    assert resp.headers["Content-Type"] == "video/mp4"

    resp = await client.get(CLIP_PATH, headers={"Range": "bytes=1000-"})
    assert resp.status == HTTPStatus.PARTIAL_CONTENT
    assert await resp.read() == CLIP_BYTES[1000:]
    assert resp.headers["Content-Range"] == f"bytes 1000-{size - 1}/{size}"

    resp = await client.get(CLIP_PATH, headers={"Range": "bytes=-10"})
    assert resp.status == HTTPStatus.PARTIAL_CONTENT
    assert await resp.read() == CLIP_BYTES[-10:]

    resp = await client.get(CLIP_PATH, headers={"Range": f"bytes={size}-"})
    assert resp.status == HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE
    assert resp.headers["Content-Range"] == f"bytes */{size}"

    resp = await client.get(CLIP_PATH, headers={"Range": "bytes=0-1,5-6"})
    assert resp.status == HTTPStatus.OK
    assert await resp.read() == CLIP_BYTES

    # The clip was only downloaded once.
    assert aioclient_mock.call_count == 1


async def test_oversized_clip_streams_full_body(
    hass: HomeAssistant, holder: SourceHolder, hass_client, aioclient_mock, monkeypatch
) -> None:
    monkeypatch.setattr(media, "CLIP_CACHE_MAX_CLIP_BYTES", 100)
    monkeypatch.setattr(media, "CHUNK_SIZE", 64)
    aioclient_mock.get(UPSTREAM_CLIP, content=CLIP_BYTES, headers={"Content-Type": "video/mp4"})
    client = await hass_client()
    resp = await client.get(CLIP_PATH, headers={"Range": "bytes=0-1"})
    assert resp.status == HTTPStatus.OK
    assert await resp.read() == CLIP_BYTES
    assert "Content-Range" not in resp.headers


def test_parse_single_range() -> None:
    parse = media._parse_single_range
    assert parse("bytes=0-9", 100) == (0, 9)
    assert parse("bytes=90-200", 100) == (90, 99)
    assert parse("bytes=-200", 100) == (0, 99)
    assert parse("bytes=5-", 100) == (5, 99)
    assert parse("bytes=9-5", 100) is None
    assert parse("bytes=-", 100) is None
    assert parse("items=0-1", 100) is None
    with pytest.raises(ValueError):
        parse("bytes=100-", 100)
    with pytest.raises(ValueError):
        parse("bytes=-0", 100)


def test_sign_item_urls_without_http(hass: HomeAssistant) -> None:
    item = {"snapshot_url": SNAPSHOT_PATH, "recording": {"status": "linked", "local_url": CLIP_PATH}}
    signed = sign_item_urls(hass, item)
    assert signed == item
    assert signed is not item


async def test_sign_item_urls_copies_and_skips_foreign_urls(hass: HomeAssistant, holder: SourceHolder) -> None:
    item = {
        "id": EVENT_ID,
        "snapshot_url": "https://example.com/snap.jpg",
        "recording": {"status": "pending", "local_url": None},
    }
    signed = sign_item_urls(hass, item)
    assert signed == item
    assert signed["recording"] is not item["recording"]

    item = {"snapshot_url": SNAPSHOT_PATH, "recording": {"status": "linked", "local_url": CLIP_PATH}}
    signed = sign_item_urls(hass, item)
    assert signed["snapshot_url"].startswith(f"{SNAPSHOT_PATH}?authSig=")
    assert signed["recording"]["local_url"].startswith(f"{CLIP_PATH}?authSig=")
    assert item["snapshot_url"] == SNAPSHOT_PATH
    assert item["recording"]["local_url"] == CLIP_PATH

    assert sign_item_urls(hass, {"snapshot_url": None, "recording": None}) == {
        "snapshot_url": None,
        "recording": None,
    }


async def test_signed_urls_from_websocket_work_without_auth(
    hass: HomeAssistant,
    holder: SourceHolder,
    hass_ws_client,
    hass_client_no_auth,
    aioclient_mock,
    hass_access_token: str,
) -> None:
    @websocket_api.websocket_command({vol.Required("type"): "frigate_feed_test/sign"})
    @websocket_api.async_response
    async def ws_sign(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict) -> None:
        item = {"snapshot_url": f"{SNAPSHOT_PATH}?height=200", "recording": {"local_url": CLIP_PATH}}
        connection.send_result(msg["id"], sign_item_urls(hass, item))

    websocket_api.async_register_command(hass, ws_sign)
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "frigate_feed_test/sign"})
    result = (await ws.receive_json())["result"]

    refresh_token = hass.auth.async_validate_access_token(hass_access_token)
    claims = jwt.decode(
        result["snapshot_url"].split("authSig=")[1], options={"verify_signature": False}
    )
    assert claims["iss"] == refresh_token.id

    aioclient_mock.get(UPSTREAM_SNAPSHOT, content=b"jpeg", headers={"Content-Type": "image/jpeg"})
    aioclient_mock.get(UPSTREAM_CLIP, content=CLIP_BYTES, headers={"Content-Type": "video/mp4"})
    client = await hass_client_no_auth()
    resp = await client.get(result["snapshot_url"])
    assert resp.status == HTTPStatus.OK
    assert await resp.read() == b"jpeg"
    resp = await client.get(result["recording"]["local_url"], headers={"Range": "bytes=0-3"})
    assert resp.status == HTTPStatus.PARTIAL_CONTENT
    assert await resp.read() == CLIP_BYTES[:4]

    # Tampering with the path or signed query invalidates the signature.
    tampered = result["recording"]["local_url"].replace("clip.mp4", "snapshot.jpg")
    assert (await client.get(tampered)).status == HTTPStatus.UNAUTHORIZED


async def test_conditional_headers_forwarded_and_304_passthrough(
    hass: HomeAssistant, holder: SourceHolder, hass_client, aioclient_mock
) -> None:
    aioclient_mock.get(UPSTREAM_SNAPSHOT, status=HTTPStatus.NOT_MODIFIED, headers={"ETag": '"abc"'})
    client = await hass_client()
    resp = await client.get(
        SNAPSHOT_PATH,
        headers={"If-None-Match": '"abc"', "If-Modified-Since": "Mon, 26 Oct 2026 10:00:00 GMT"},
    )
    assert resp.status == HTTPStatus.NOT_MODIFIED
    sent = _sent_headers(aioclient_mock)
    assert sent["If-None-Match"] == '"abc"'
    assert sent["If-Modified-Since"] == "Mon, 26 Oct 2026 10:00:00 GMT"


async def test_content_length_dropped_for_encoded_upstream(
    hass: HomeAssistant, holder: SourceHolder, hass_client, aioclient_mock
) -> None:
    aioclient_mock.get(
        UPSTREAM_SNAPSHOT,
        content=b"decoded-body",
        headers={"Content-Type": "image/jpeg", "Content-Encoding": "gzip", "Content-Length": "5"},
    )
    client = await hass_client()
    resp = await client.get(SNAPSHOT_PATH)
    assert resp.status == HTTPStatus.OK
    assert await resp.read() == b"decoded-body"


async def test_cached_clip_purged_after_ttl(
    hass: HomeAssistant, holder: SourceHolder, hass_client, aioclient_mock
) -> None:
    aioclient_mock.get(UPSTREAM_CLIP, content=CLIP_BYTES, headers={"Content-Type": "video/mp4"})
    client = await hass_client()
    resp = await client.get(CLIP_PATH, headers={"Range": "bytes=0-1"})
    assert resp.status == HTTPStatus.PARTIAL_CONTENT
    cache = hass.data[media.DATA_CLIP_CACHE]
    assert cache._items

    later = time.monotonic() + media.CLIP_CACHE_TTL_SECONDS + 5
    with patch.object(media.time, "monotonic", return_value=later):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=media.CLIP_CACHE_TTL_SECONDS + 5))
        await hass.async_block_till_done()
    assert not cache._items
