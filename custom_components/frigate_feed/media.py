"""Authenticated media proxy for Frigate snapshots, review thumbnails and clips."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from http import HTTPStatus
import logging
from pathlib import PurePosixPath
import re
import time
from typing import Any
from urllib.parse import quote

import aiohttp
from aiohttp import hdrs, web

from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.auth import async_sign_path
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later

from .const import (
    DOMAIN,
    MEDIA_KIND_CLIP,
    MEDIA_KIND_SNAPSHOT,
    MEDIA_KINDS,
    MEDIA_URL,
    REVIEW_MEDIA_KINDS,
    REVIEW_MEDIA_URL,
    SIGNED_URL_EXPIRY,
)
from .feed import clip_bounds
from .frigate_source import FrigateSource

_LOGGER = logging.getLogger(__name__)

DATA_MEDIA_VIEWS_REGISTERED = f"{DOMAIN}_media_views_registered"
DATA_CLIP_CACHE = f"{DOMAIN}_clip_cache"
MEDIA_PATH_PREFIX = "/api/frigate_feed/media/"

EVENT_ID_RE = re.compile(r"^[0-9]+\.[0-9]+-[a-z0-9]+$", re.IGNORECASE)
SNAPSHOT_QUERY_PARAMS: tuple[str, ...] = ("height", "quality")
_SAFE_PARAM_VALUE_RE = re.compile(r"^[0-9]{1,5}$")

FORWARDED_REQUEST_HEADERS: tuple[str, ...] = (
    hdrs.RANGE,
    hdrs.IF_RANGE,
    hdrs.IF_NONE_MATCH,
    hdrs.IF_MODIFIED_SINCE,
)
COPIED_RESPONSE_HEADERS: tuple[str, ...] = (
    hdrs.CONTENT_TYPE,
    hdrs.CONTENT_LENGTH,
    hdrs.CONTENT_RANGE,
    hdrs.ACCEPT_RANGES,
    hdrs.CACHE_CONTROL,
    hdrs.LAST_MODIFIED,
    hdrs.ETAG,
)
PASSTHROUGH_STATUSES: frozenset[int] = frozenset(
    (HTTPStatus.OK, HTTPStatus.PARTIAL_CONTENT, HTTPStatus.NOT_MODIFIED, HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
)

UPSTREAM_TIMEOUT = aiohttp.ClientTimeout(total=None, connect=10, sock_read=60)
CHUNK_SIZE = 64 * 1024

# Frigate (0.14-0.16) builds clip.mp4 on the fly and answers 200 with chunked
# encoding, ignoring Range. Browsers need 206 responses to seek (Safari/iOS will
# not play the clip at all without them), so when a client asks for a range and
# upstream ignores it, the clip is buffered in a small, short-lived memory cache
# and ranges are served from there.
CLIP_CACHE_MAX_CLIP_BYTES = 64 * 1024 * 1024
CLIP_CACHE_MAX_TOTAL_BYTES = 128 * 1024 * 1024
CLIP_CACHE_TTL_SECONDS = 300.0

_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")


def async_register_media_views(
    hass: HomeAssistant, get_source: Callable[[], FrigateSource | None]
) -> None:
    """Register the media proxy views once (idempotent).

    Both views require HA auth (signed paths work) and stream from Frigate using
    `source.auth_headers()` and `source.verify_ssl`:
    - MEDIA_URL ("/api/frigate_feed/media/{event_id}/{kind}", MEDIA_KINDS) streams
      `{source.base_url}/api/events/{event_id}/{kind}`.
    - REVIEW_MEDIA_URL ("/api/frigate_feed/media/review/{review_id}/{kind}",
      REVIEW_MEDIA_KINDS) looks the review item up in Frigate and streams the camera
      recording over its window (`clip.mp4`) or its thumbnail (`thumb.webp`).
    Range/If-Range request headers and the upstream status are passed through. 404
    when the source is unavailable or the id is unknown, 502 on upstream errors.
    """
    if hass.data.get(DATA_MEDIA_VIEWS_REGISTERED):
        return
    hass.data[DATA_MEDIA_VIEWS_REGISTERED] = True
    hass.http.register_view(FrigateFeedMediaView(hass, get_source))
    hass.http.register_view(FrigateFeedReviewMediaView(hass, get_source))


def sign_item_urls(hass: HomeAssistant, item: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of an item dict with snapshot_url and recording.local_url signed.

    Must be called while handling a websocket command so async_sign_path can bind
    the signature to the current connection's refresh token. Uses SIGNED_URL_EXPIRY.
    """
    signed = dict(item)
    snapshot_url = signed.get("snapshot_url")
    if _is_media_path(snapshot_url):
        signed["snapshot_url"] = _sign_path(hass, snapshot_url)
    recording = signed.get("recording")
    if isinstance(recording, dict):
        recording = signed["recording"] = dict(recording)
        if _is_media_path(recording.get("local_url")):
            recording["local_url"] = _sign_path(hass, recording["local_url"])
    return signed


def _is_media_path(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(MEDIA_PATH_PREFIX)


@callback
def _sign_path(hass: HomeAssistant, path: str) -> str:
    """Sign a media path.

    async_sign_path uses websocket_api.current_connection (set for the duration of a
    websocket command) or the current HTTP request to pick the refresh token, and
    falls back to HA's read-only content user. It only fails when the http
    component's auth has not been set up; then the unsigned path is returned.
    """
    try:
        return async_sign_path(hass, path, SIGNED_URL_EXPIRY)
    except KeyError:
        _LOGGER.debug("Cannot sign %s: HTTP auth not set up; returning unsigned path", path)
        return path


def is_valid_event_id(event_id: str) -> bool:
    """Return True for Frigate event ids like `1793016151.051489-lzerqk`."""
    return len(event_id) <= 64 and EVENT_ID_RE.fullmatch(event_id) is not None


def _snapshot_params(request: web.Request) -> dict[str, str]:
    params: dict[str, str] = {}
    for key in SNAPSHOT_QUERY_PARAMS:
        value = request.query.get(key)
        if value is not None and _SAFE_PARAM_VALUE_RE.fullmatch(value):
            params[key] = value
    return params


def _parse_single_range(value: str, size: int) -> tuple[int, int] | None:
    """Parse a single `bytes=` range into inclusive (start, end) bounds.

    Returns None for unsupported/malformed headers (served as a full 200) and
    raises ValueError when the range cannot be satisfied for `size`.
    """
    match = _RANGE_RE.fullmatch(value.strip())
    if match is None:
        return None
    first, last = match.groups()
    if not first and not last:
        return None
    if not first:
        suffix = int(last)
        if suffix == 0 or size == 0:
            raise ValueError("unsatisfiable range")
        return max(size - suffix, 0), size - 1
    start = int(first)
    end = int(last) if last else size - 1
    if start >= size:
        raise ValueError("unsatisfiable range")
    if end < start:
        return None
    return start, min(end, size - 1)


@dataclass
class _CachedClip:
    data: bytes
    content_type: str
    expires: float


class _ClipCache:
    """Tiny LRU cache with sliding TTL for clips whose upstream ignores Range.

    `lock` serializes buffering downloads across all clips, so at most one clip is
    held in memory outside the cache (bounding memory to the per-clip limit plus
    CLIP_CACHE_MAX_TOTAL_BYTES) and concurrent range requests for the same
    uncached clip trigger a single download.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.lock = asyncio.Lock()
        self._items: OrderedDict[str, _CachedClip] = OrderedDict()
        self._cancel_timer: CALLBACK_TYPE | None = None
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._async_on_stop)

    def get(self, key: str) -> _CachedClip | None:
        self._expire()
        clip = self._items.get(key)
        if clip is not None:
            # Sliding expiry: keep the clip while it is being played/seeked.
            clip.expires = time.monotonic() + CLIP_CACHE_TTL_SECONDS
            self._items.move_to_end(key)
        return clip

    def put(self, key: str, data: bytes, content_type: str) -> _CachedClip:
        self._expire()
        clip = _CachedClip(data, content_type, time.monotonic() + CLIP_CACHE_TTL_SECONDS)
        self._items.pop(key, None)
        self._items[key] = clip
        total = sum(len(item.data) for item in self._items.values())
        while total > CLIP_CACHE_MAX_TOTAL_BYTES and len(self._items) > 1:
            _, evicted = self._items.popitem(last=False)
            total -= len(evicted.data)
        self._schedule_purge()
        return clip

    def clear(self) -> None:
        self._items.clear()
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None

    def _expire(self) -> None:
        now = time.monotonic()
        for key in [key for key, item in self._items.items() if item.expires <= now]:
            del self._items[key]

    def _schedule_purge(self) -> None:
        if self._cancel_timer is not None or not self._items:
            return
        delay = max(min(item.expires for item in self._items.values()) - time.monotonic(), 0) + 1
        self._cancel_timer = async_call_later(self.hass, delay, self._async_on_timer)

    @callback
    def _async_on_timer(self, _now: Any) -> None:
        self._cancel_timer = None
        self._expire()
        self._schedule_purge()

    @callback
    def _async_on_stop(self, _event: Event) -> None:
        self.clear()


class FrigateFeedMediaView(HomeAssistantView):
    """Proxy Frigate event snapshots and clips behind HA authentication."""

    url = MEDIA_URL
    name = "api:frigate_feed:media"
    requires_auth = True

    def __init__(self, hass: HomeAssistant, get_source: Callable[[], FrigateSource | None]) -> None:
        self.hass = hass
        self._get_source = get_source
        self._clip_cache: _ClipCache = hass.data.get(DATA_CLIP_CACHE)
        if self._clip_cache is None:
            self._clip_cache = hass.data[DATA_CLIP_CACHE] = _ClipCache(hass)

    async def get(self, request: web.Request, event_id: str, kind: str) -> web.StreamResponse:
        """Stream one event media file from Frigate."""
        if kind not in MEDIA_KINDS or not is_valid_event_id(event_id):
            return web.Response(status=HTTPStatus.NOT_FOUND)
        source = self._get_source()
        if source is None:
            return web.Response(status=HTTPStatus.NOT_FOUND)
        return await self._async_serve(
            request, source, f"/api/events/{event_id}/{kind}", kind, f"event|{event_id}"
        )

    async def _async_serve(
        self,
        request: web.Request,
        source: FrigateSource,
        upstream_path: str,
        kind: str,
        cache_id: str,
    ) -> web.StreamResponse:
        """Proxy `{source.base_url}{upstream_path}`; clip ranges go through the clip cache."""
        try:
            base_url = source.base_url
            auth_headers = source.auth_headers()
            verify_ssl = source.verify_ssl
        except Exception:  # noqa: BLE001 - source not ready behaves like unavailable
            _LOGGER.debug("Frigate source not ready for media request", exc_info=True)
            return web.Response(status=HTTPStatus.NOT_FOUND)

        upstream_url = f"{base_url.rstrip('/')}{upstream_path}"
        params = _snapshot_params(request) if kind == MEDIA_KIND_SNAPSHOT else {}
        headers = dict(auth_headers)
        for name in FORWARDED_REQUEST_HEADERS:
            if (value := request.headers.get(name)) is not None:
                headers[name] = value

        session = async_get_clientsession(self.hass, verify_ssl=verify_ssl)
        range_header = request.headers.get(hdrs.RANGE)
        if kind == MEDIA_KIND_CLIP and range_header and hdrs.IF_RANGE not in request.headers:
            cache_key = f"{base_url}|{cache_id}"
            return await self._async_serve_clip_range(
                request, session, upstream_url, headers, cache_key, range_header
            )
        return await self._async_proxy(request, session, upstream_url, params, headers)

    async def _async_proxy(
        self,
        request: web.Request,
        session: aiohttp.ClientSession,
        url: str,
        params: dict[str, str],
        headers: dict[str, str],
    ) -> web.StreamResponse:
        try:
            async with session.get(
                url, params=params or None, headers=headers, timeout=UPSTREAM_TIMEOUT
            ) as upstream:
                if (error := _map_upstream_status(upstream.status, url)) is not None:
                    return error
                return await _async_stream_upstream(request, upstream)
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("Frigate media request %s failed: %s", url, err)
            return web.Response(status=HTTPStatus.BAD_GATEWAY)

    async def _async_serve_clip_range(
        self,
        request: web.Request,
        session: aiohttp.ClientSession,
        url: str,
        headers: dict[str, str],
        cache_key: str,
        range_header: str,
    ) -> web.StreamResponse:
        """Serve a byte range of a clip, buffering when upstream ignores Range.

        The buffering lock is released before anything is streamed through, so a
        long passthrough never blocks other range requests.
        """
        cache = self._clip_cache
        if (clip := cache.get(cache_key)) is not None:
            return _range_response(clip, range_header)
        await cache.lock.acquire()
        locked = True
        try:
            if (clip := cache.get(cache_key)) is not None:
                return _range_response(clip, range_header)
            try:
                async with session.get(url, headers=headers, timeout=UPSTREAM_TIMEOUT) as upstream:
                    if (error := _map_upstream_status(upstream.status, url)) is not None:
                        return error
                    if (
                        upstream.status != HTTPStatus.OK
                        or hdrs.CONTENT_RANGE in upstream.headers
                        or hdrs.CONTENT_ENCODING in upstream.headers
                    ):
                        # Upstream honours Range (or answered 304/416): pass through.
                        cache.lock.release()
                        locked = False
                        return await _async_stream_upstream(request, upstream)
                    # The mocked client in tests returns a fresh stream per access.
                    content = upstream.content
                    body, complete = await _async_read_limited(
                        upstream, content, CLIP_CACHE_MAX_CLIP_BYTES
                    )
                    if not complete:
                        cache.lock.release()
                        locked = False
                        return await _async_stream_upstream(request, upstream, body, content)
                    content_type = upstream.headers.get(hdrs.CONTENT_TYPE, "video/mp4")
            except (aiohttp.ClientError, TimeoutError) as err:
                _LOGGER.debug("Frigate clip request %s failed: %s", url, err)
                return web.Response(status=HTTPStatus.BAD_GATEWAY)
            clip = cache.put(cache_key, body, content_type)
            return _range_response(clip, range_header)
        finally:
            if locked:
                cache.lock.release()


class FrigateFeedReviewMediaView(FrigateFeedMediaView):
    """Proxy the recording clip and thumbnail of a Frigate review item."""

    url = REVIEW_MEDIA_URL
    name = "api:frigate_feed:review_media"

    async def get(self, request: web.Request, review_id: str, kind: str) -> web.StreamResponse:  # type: ignore[override]
        """Stream a review item's clip or thumbnail from Frigate."""
        if kind not in REVIEW_MEDIA_KINDS or not is_valid_event_id(review_id):
            return web.Response(status=HTTPStatus.NOT_FOUND)
        source = self._get_source()
        if source is None:
            return web.Response(status=HTTPStatus.NOT_FOUND)
        cache_id = f"review|{review_id}"
        if kind == MEDIA_KIND_CLIP and (range_header := request.headers.get(hdrs.RANGE)):
            # Seeking re-requests ranges; serve buffered clips without a review lookup.
            try:
                cached = self._clip_cache.get(f"{source.base_url}|{cache_id}")
            except Exception:  # noqa: BLE001 - source not ready behaves like unavailable
                cached = None
            if cached is not None and hdrs.IF_RANGE not in request.headers:
                return _range_response(cached, range_header)
        review = await source.async_get_review(review_id)
        if review is None:
            return web.Response(status=HTTPStatus.NOT_FOUND)
        upstream_path = (
            _review_clip_path(review, source) if kind == MEDIA_KIND_CLIP else _review_thumb_path(review)
        )
        if upstream_path is None:
            return web.Response(status=HTTPStatus.NOT_FOUND)
        return await self._async_serve(request, source, upstream_path, kind, cache_id)


def _review_clip_path(review: dict[str, Any], source: FrigateSource) -> str | None:
    """Frigate recordings clip path over the padded window of an ended review item."""
    camera = review.get("camera")
    try:
        start = float(review["start_time"])
        end = float(review["end_time"])
    except (KeyError, TypeError, ValueError):
        return None
    if not isinstance(camera, str) or not camera:
        return None
    clip_start, clip_end = clip_bounds(review, source, start, end)
    return f"/api/{quote(camera, safe='')}/start/{clip_start}/end/{clip_end}/clip.mp4"


def _review_thumb_path(review: dict[str, Any]) -> str | None:
    """Frigate serves review thumbnails from /clips/review/ (thumb_path is a container path)."""
    thumb_path = review.get("thumb_path")
    if not isinstance(thumb_path, str) or not thumb_path:
        return None
    name = PurePosixPath(thumb_path).name
    if not name.endswith(".webp"):
        return None
    return f"/clips/review/{quote(name, safe='')}"


def _map_upstream_status(status: int, url: str) -> web.Response | None:
    """Return an error response for upstream statuses that are not passed through."""
    if status == HTTPStatus.NOT_FOUND:
        return web.Response(status=HTTPStatus.NOT_FOUND)
    if status not in PASSTHROUGH_STATUSES:
        _LOGGER.debug("Frigate media request %s returned HTTP %s", url, status)
        return web.Response(status=HTTPStatus.BAD_GATEWAY)
    return None


async def _async_read_limited(
    upstream: aiohttp.ClientResponse, content: aiohttp.StreamReader, limit: int
) -> tuple[bytes, bool]:
    """Read the upstream body up to `limit` bytes; return (data, reached_eof)."""
    length = upstream.headers.get(hdrs.CONTENT_LENGTH)
    if length is not None and length.isdigit() and int(length) > limit:
        return b"", False
    chunks: list[bytes] = []
    size = 0
    async for chunk in content.iter_chunked(CHUNK_SIZE):
        chunks.append(chunk)
        size += len(chunk)
        if size > limit:
            return b"".join(chunks), False
    return b"".join(chunks), True


async def _async_stream_upstream(
    request: web.Request,
    upstream: aiohttp.ClientResponse,
    prefix: bytes = b"",
    content: aiohttp.StreamReader | None = None,
) -> web.StreamResponse:
    """Copy upstream status, relevant headers and body to the client.

    `prefix` holds body bytes already consumed from `content` (upstream.content).
    """
    if content is None:
        content = upstream.content
    response = web.StreamResponse(status=upstream.status)
    for name in COPIED_RESPONSE_HEADERS:
        if (value := upstream.headers.get(name)) is not None:
            response.headers[name] = value
    if hdrs.CONTENT_ENCODING in upstream.headers:
        # The client session decompresses the body, so the upstream length is wrong.
        response.headers.pop(hdrs.CONTENT_LENGTH, None)
    try:
        await response.prepare(request)
        if prefix:
            await response.write(prefix)
        async for chunk in content.iter_chunked(CHUNK_SIZE):
            await response.write(chunk)
        await response.write_eof()
    except (aiohttp.ClientError, ConnectionError, TimeoutError) as err:
        # Client disconnect or upstream failure after the headers were sent: abort
        # the connection so a truncated body is never mistaken for a complete one.
        _LOGGER.debug("Streaming %s aborted: %r", request.path, err)
        if request.transport is not None:
            request.transport.close()
    return response


def _range_response(clip: _CachedClip, range_header: str) -> web.Response:
    """Build a 206/200/416 response for a cached clip."""
    size = len(clip.data)
    headers = {
        hdrs.ACCEPT_RANGES: "bytes",
        hdrs.CACHE_CONTROL: "no-store",
        hdrs.CONTENT_TYPE: clip.content_type,
    }
    try:
        bounds = _parse_single_range(range_header, size)
    except ValueError:
        headers.pop(hdrs.CONTENT_TYPE)
        headers[hdrs.CONTENT_RANGE] = f"bytes */{size}"
        return web.Response(status=HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, headers=headers)
    if bounds is None:
        return web.Response(status=HTTPStatus.OK, body=clip.data, headers=headers)
    start, end = bounds
    headers[hdrs.CONTENT_RANGE] = f"bytes {start}-{end}/{size}"
    return web.Response(status=HTTPStatus.PARTIAL_CONTENT, body=clip.data[start : end + 1], headers=headers)
