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
from yarl import URL

from homeassistant.components import websocket_api
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
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
REVIEW_ID = "1793016150.900000-rev001"
REVIEW = {
    "id": REVIEW_ID,
    "camera": "front_door",
    "start_time": 1793016150.9,
    "end_time": 1793016170.2,
    "severity": "alert",
    "thumb_path": f"/media/frigate/clips/review/thumb-front_door-{REVIEW_ID}.webp",
    "data": {"detections": [EVENT_ID], "objects": ["person"]},
}
REVIEW_CLIP_PATH = f"/api/frigate_feed/media/review/{REVIEW_ID}/clip.mp4"
REVIEW_THUMB_PATH = f"/api/frigate_feed/media/review/{REVIEW_ID}/thumb.webp"
# start 150.9 - pre_capture 5 -> floor 145; end 170.2 + post_capture 8 -> ceil 179
UPSTREAM_REVIEW_CLIP = f"{BASE_URL}/api/front_door/start/1793016145/end/1793016179/clip.mp4"
UPSTREAM_REVIEW_THUMB = f"{BASE_URL}/clips/review/thumb-front_door-{REVIEW_ID}.webp"


class FakeSource:
    """Implements the parts of the FrigateSource contract used by media.py."""

    def __init__(self, base_url: str = BASE_URL, verify_ssl: bool = True) -> None:
        self.base_url = base_url
        self.verify_ssl = verify_ssl
        self.reviews: dict[str, dict[str, Any]] = {REVIEW_ID: dict(REVIEW)}
        self.review_lookups = 0

    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer frigate-token"}

    def pre_capture_seconds(self, camera: str, severity: str | None = None) -> float:
        return 5.0

    def post_capture_seconds(self, camera: str, severity: str | None = None) -> float:
        return 8.0

    async def async_get_review(self, review_id: str) -> dict[str, Any] | None:
        self.review_lookups += 1
        return self.reviews.get(review_id)


class SourceHolder:
    def __init__(self) -> None:
        self.source: FakeSource | None = FakeSource()

    def __call__(self) -> FakeSource | None:
        return self.source


@pytest.fixture(autouse=True)
def clip_dir(tmp_path, monkeypatch) -> Any:
    """Keep clip files of each test in its own temporary directory."""
    monkeypatch.setattr(media.tempfile, "gettempdir", lambda: str(tmp_path))
    return tmp_path / media.CLIP_CACHE_DIR_NAME


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
    resp = await client.get(f"{SNAPSHOT_PATH}?height=200&quality=70&evil=1&crop=1&bbox=yes")
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
    assert dict(url.query) == {"height": "200", "quality": "70", "crop": "1"}
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

    # Multiple ranges are not supported by aiohttp's FileResponse (browsers never ask).
    resp = await client.get(CLIP_PATH, headers={"Range": "bytes=0-1,5-6"})
    assert resp.status == HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE

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
    snapshot_query = URL(result["snapshot_url"]).query
    claims = jwt.decode(snapshot_query["authSig"], options={"verify_signature": False})
    assert claims["iss"] == refresh_token.id
    # HA leaves `height` out of signatures and signed URLs; sign_item_urls keeps it.
    assert snapshot_query["height"] == "200"

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
    hass: HomeAssistant, holder: SourceHolder, hass_client, aioclient_mock, clip_dir
) -> None:
    aioclient_mock.get(UPSTREAM_CLIP, content=CLIP_BYTES, headers={"Content-Type": "video/mp4"})
    client = await hass_client()
    resp = await client.get(CLIP_PATH, headers={"Range": "bytes=0-1"})
    assert resp.status == HTTPStatus.PARTIAL_CONTENT
    cache = hass.data[media.DATA_CLIP_CACHE]
    (clip,) = cache._items.values()
    assert clip.path.parent == clip_dir
    assert clip.path.read_bytes() == CLIP_BYTES

    later = time.monotonic() + media.CLIP_CACHE_TTL_SECONDS + 5
    with patch.object(media.time, "monotonic", return_value=later):
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=media.CLIP_CACHE_TTL_SECONDS + 5))
        await hass.async_block_till_done()
    assert not cache._items
    assert not clip.path.exists()


async def test_clip_files_evicted_over_total_limit_and_removed_on_stop(
    hass: HomeAssistant, holder: SourceHolder, hass_client, aioclient_mock, monkeypatch, clip_dir
) -> None:
    monkeypatch.setattr(media, "CLIP_CACHE_MAX_TOTAL_BYTES", len(CLIP_BYTES) + 10)
    aioclient_mock.get(UPSTREAM_CLIP, content=CLIP_BYTES, headers={"Content-Type": "video/mp4"})
    aioclient_mock.get(UPSTREAM_REVIEW_CLIP, content=CLIP_BYTES, headers={"Content-Type": "video/mp4"})
    client = await hass_client()
    assert (await client.get(CLIP_PATH, headers={"Range": "bytes=0-1"})).status == HTTPStatus.PARTIAL_CONTENT
    cache = hass.data[media.DATA_CLIP_CACHE]
    (first,) = cache._items.values()
    assert (await client.get(REVIEW_CLIP_PATH, headers={"Range": "bytes=0-1"})).status == HTTPStatus.PARTIAL_CONTENT
    await hass.async_block_till_done()
    (second,) = cache._items.values()
    assert not first.path.exists()
    assert second.path.exists()

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert not cache._items
    assert not second.path.exists()


async def test_stale_clip_files_removed_on_first_download(
    hass: HomeAssistant, holder: SourceHolder, hass_client, aioclient_mock, clip_dir
) -> None:
    clip_dir.mkdir(parents=True, exist_ok=True)
    stale = clip_dir / "left-over.mp4"
    stale.write_bytes(b"old")
    aioclient_mock.get(UPSTREAM_CLIP, content=CLIP_BYTES, headers={"Content-Type": "video/mp4"})
    client = await hass_client()
    assert (await client.get(CLIP_PATH, headers={"Range": "bytes=0-1"})).status == HTTPStatus.PARTIAL_CONTENT
    assert not stale.exists()


async def test_review_clip_proxies_recording_window(
    hass: HomeAssistant, holder: SourceHolder, hass_client, aioclient_mock
) -> None:
    aioclient_mock.get(UPSTREAM_REVIEW_CLIP, content=CLIP_BYTES, headers={"Content-Type": "video/mp4"})
    client = await hass_client()
    resp = await client.get(REVIEW_CLIP_PATH)
    assert resp.status == HTTPStatus.OK
    assert await resp.read() == CLIP_BYTES
    assert _sent_headers(aioclient_mock)["Authorization"] == "Bearer frigate-token"


async def test_review_clip_ranges_buffered_without_repeated_lookups(
    hass: HomeAssistant, holder: SourceHolder, hass_client, aioclient_mock
) -> None:
    aioclient_mock.get(UPSTREAM_REVIEW_CLIP, content=CLIP_BYTES, headers={"Content-Type": "video/mp4"})
    client = await hass_client()
    resp = await client.get(REVIEW_CLIP_PATH, headers={"Range": "bytes=0-1"})
    assert resp.status == HTTPStatus.PARTIAL_CONTENT
    assert await resp.read() == CLIP_BYTES[:2]
    resp = await client.get(REVIEW_CLIP_PATH, headers={"Range": "bytes=1000-"})
    assert resp.status == HTTPStatus.PARTIAL_CONTENT
    assert await resp.read() == CLIP_BYTES[1000:]
    assert aioclient_mock.call_count == 1
    assert holder.source.review_lookups == 1


async def test_review_thumbnail(hass: HomeAssistant, holder: SourceHolder, hass_client, aioclient_mock) -> None:
    aioclient_mock.get(UPSTREAM_REVIEW_THUMB, content=b"webp", headers={"Content-Type": "image/webp"})
    client = await hass_client()
    resp = await client.get(REVIEW_THUMB_PATH)
    assert resp.status == HTTPStatus.OK
    assert await resp.read() == b"webp"
    assert resp.headers["Content-Type"] == "image/webp"


@pytest.mark.parametrize(
    ("review", "path"),
    [
        (None, REVIEW_CLIP_PATH),
        ({**REVIEW, "end_time": None}, REVIEW_CLIP_PATH),
        ({**REVIEW, "camera": None}, REVIEW_CLIP_PATH),
        ({**REVIEW, "thumb_path": None}, REVIEW_THUMB_PATH),
        ({**REVIEW, "thumb_path": "/media/frigate/clips/review/thumb.jpg"}, REVIEW_THUMB_PATH),
    ],
    ids=["unknown", "in_progress", "no_camera", "no_thumb", "not_webp"],
)
async def test_review_media_not_found(
    hass: HomeAssistant, holder: SourceHolder, hass_client, aioclient_mock, review, path: str
) -> None:
    if review is None:
        holder.source.reviews.clear()
    else:
        holder.source.reviews[REVIEW_ID] = review
    client = await hass_client()
    resp = await client.get(path)
    assert resp.status == HTTPStatus.NOT_FOUND
    assert aioclient_mock.call_count == 0


@pytest.mark.parametrize(
    "path",
    [
        f"/api/frigate_feed/media/review/{REVIEW_ID}/snapshot.jpg",
        "/api/frigate_feed/media/review/not-an-id/clip.mp4",
    ],
)
async def test_review_invalid_kind_or_id(
    hass: HomeAssistant, holder: SourceHolder, hass_client, aioclient_mock, path: str
) -> None:
    client = await hass_client()
    resp = await client.get(path)
    assert resp.status == HTTPStatus.NOT_FOUND
    assert holder.source.review_lookups == 0
    assert aioclient_mock.call_count == 0


async def test_review_media_requires_auth(hass: HomeAssistant, holder: SourceHolder, hass_client_no_auth) -> None:
    client = await hass_client_no_auth()
    resp = await client.get(REVIEW_CLIP_PATH)
    assert resp.status == HTTPStatus.UNAUTHORIZED
