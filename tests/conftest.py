"""Shared fixtures for Frigate Feed tests.

`fake_frigate` emulates what the official Frigate integration leaves in hass.data:
    hass.data["frigate"][entry_id] = {"client": FakeFrigateClient, "config": <api/config>}
Fixture events and review items are rebased together so the newest event started 30s
ago (offsets preserved); the oldest fixture event and review item are ~32h older and
therefore outside the default 24h retention.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
import json
from pathlib import Path
import time
from typing import Any
import warnings

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

warnings.filterwarnings(
    "ignore",
    message="Inheritance class HomeAssistantApplication from web.Application is discouraged",
    category=DeprecationWarning,
    module=r"homeassistant\.components\.http\.__init__",
)

FIXTURES = Path(__file__).parent / "fixtures"
FRIGATE_ENTRY_ID = "frigate_entry_1"
FRIGATE_URL = "http://frigate.test:5000"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow loading custom_components/frigate_feed in every test."""
    yield


class FakeFrigateClientError(Exception):
    """Stand-in for custom_components.frigate.api.FrigateApiClientError."""


@dataclass
class FakeFrigateClient:
    """Minimal FrigateApiClient double (same method signatures as v5.15.6)."""

    events: list[dict[str, Any]]
    config: dict[str, Any]
    reviews: list[dict[str, Any]] = field(default_factory=list)
    fail: bool = False
    calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def async_get_events(
        self,
        cameras: list[str] | None = None,
        labels: list[str] | None = None,
        sub_labels: list[str] | None = None,
        zones: list[str] | None = None,
        after: int | None = None,
        before: int | None = None,
        limit: int | None = None,
        has_clip: bool | None = None,
        has_snapshot: bool | None = None,
        favorites: bool | None = None,
        decode_json: bool = True,
    ) -> list[dict[str, Any]]:
        self.calls.append(
            (
                "async_get_events",
                {"cameras": cameras, "labels": labels, "after": after, "before": before, "limit": limit},
            )
        )
        if self.fail:
            raise FakeFrigateClientError("frigate unavailable")
        result = []
        for event in self.events:
            if cameras and event["camera"] not in cameras:
                continue
            if labels and event["label"] not in labels:
                continue
            if after is not None and event["start_time"] < after:
                continue
            if before is not None and event["start_time"] > before:
                continue
            if has_clip is not None and bool(event["has_clip"]) != bool(has_clip):
                continue
            if has_snapshot is not None and bool(event["has_snapshot"]) != bool(has_snapshot):
                continue
            result.append(copy.deepcopy(event))
        result.sort(key=lambda event: event["start_time"], reverse=True)
        if limit is not None:
            result = result[:limit]
        return result

    async def async_get_event(self, event_id: str, decode_json: bool = True) -> dict[str, Any]:
        self.calls.append(("async_get_event", {"event_id": event_id}))
        if self.fail:
            raise FakeFrigateClientError("frigate unavailable")
        for event in self.events:
            if event["id"] == event_id:
                return copy.deepcopy(event)
        raise FakeFrigateClientError("Event not found")

    async def async_get_reviews(
        self,
        cameras: list[str] | None = None,
        labels: list[str] | None = None,
        zones: list[str] | None = None,
        severity: str | None = None,
        after: float | None = None,
        before: float | None = None,
        limit: int | None = None,
        reviewed: bool | None = None,
        decode_json: bool = True,
    ) -> list[dict[str, Any]]:
        self.calls.append(
            (
                "async_get_reviews",
                {"cameras": cameras, "labels": labels, "after": after, "before": before, "limit": limit},
            )
        )
        if self.fail:
            raise FakeFrigateClientError("frigate unavailable")
        result = []
        for review in self.reviews:
            if cameras and review["camera"] not in cameras:
                continue
            if labels and not set(labels) & set(review["data"]["objects"]):
                continue
            if after is not None and review["start_time"] < after:
                continue
            if before is not None and review["start_time"] > before:
                continue
            result.append(copy.deepcopy(review))
        result.sort(key=lambda review: review["start_time"], reverse=True)
        if limit is not None:
            result = result[:limit]
        return result

    async def api_wrapper(self, method: str, url: str, **kwargs: Any) -> Any:
        """Only GET /api/review/{id} is used (FrigateSource.async_get_review)."""
        self.calls.append(("api_wrapper", {"method": method, "url": url}))
        if self.fail:
            raise FakeFrigateClientError("frigate unavailable")
        prefix = f"{FRIGATE_URL}/api/review/"
        if method == "get" and url.startswith(prefix):
            review_id = url[len(prefix) :]
            for review in self.reviews:
                if review["id"] == review_id:
                    return copy.deepcopy(review)
        raise FakeFrigateClientError("Review not found")

    def get_auth_headers(self) -> dict[str, str]:
        return {"Authorization": "Bearer test-token"}


@dataclass
class FakeFrigate:
    entry: MockConfigEntry
    client: FakeFrigateClient
    config: dict[str, Any]
    events: list[dict[str, Any]]
    reviews: list[dict[str, Any]]


def _load_json(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def rebase_events(
    events: list[dict[str, Any]], newest_start: float, latest: float | None = None
) -> list[dict[str, Any]]:
    """Shift event (or review item) times so `latest` (default: the newest start) moves to `newest_start`."""
    shifted = copy.deepcopy(events)
    if latest is None:
        latest = max(event["start_time"] for event in shifted)
    delta = newest_start - latest
    for event in shifted:
        event["start_time"] += delta
        if event.get("end_time") is not None:
            event["end_time"] += delta
    return shifted


@pytest.fixture
def frigate_config() -> dict[str, Any]:
    return _load_json("frigate_config.json")


@pytest.fixture
def frigate_events() -> list[dict[str, Any]]:
    return rebase_events(_load_json("frigate_events.json"), time.time() - 30)


@pytest.fixture
def frigate_reviews(frigate_events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Review item fixtures, shifted by the same amount as the events."""
    raw_latest = max(event["start_time"] for event in _load_json("frigate_events.json"))
    newest = max(event["start_time"] for event in frigate_events)
    return rebase_events(_load_json("frigate_reviews.json"), newest, raw_latest)


@pytest.fixture
def fake_frigate(
    hass: HomeAssistant,
    frigate_config: dict[str, Any],
    frigate_events: list[dict[str, Any]],
    frigate_reviews: list[dict[str, Any]],
) -> FakeFrigate:
    """Install a loaded Frigate integration entry with fake runtime data."""
    entry = MockConfigEntry(
        domain="frigate",
        entry_id=FRIGATE_ENTRY_ID,
        title="Frigate",
        data={"url": FRIGATE_URL, "validate_ssl": True},
    )
    entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.LOADED)
    client = FakeFrigateClient(events=frigate_events, config=frigate_config, reviews=frigate_reviews)
    hass.data.setdefault("frigate", {})[entry.entry_id] = {"client": client, "config": frigate_config}
    return FakeFrigate(
        entry=entry, client=client, config=frigate_config, events=frigate_events, reviews=frigate_reviews
    )
