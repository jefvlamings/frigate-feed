"""Tests for the Frigate source adapter."""

from __future__ import annotations

import time
from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.frigate_feed.const import DEFAULT_PRE_CAPTURE_SECONDS
from custom_components.frigate_feed.frigate_source import (
    FrigateNotReady,
    FrigateSource,
    async_get_frigate_source,
    frigate_entries,
)

from .conftest import FRIGATE_ENTRY_ID, FRIGATE_URL, FakeFrigate, FakeFrigateClientError


def _source(hass: HomeAssistant) -> FrigateSource:
    return async_get_frigate_source(hass, FRIGATE_ENTRY_ID)


def _runtime(hass: HomeAssistant) -> dict[str, Any]:
    return hass.data["frigate"][FRIGATE_ENTRY_ID]


async def test_frigate_entries(hass: HomeAssistant, fake_frigate: FakeFrigate) -> None:
    MockConfigEntry(domain="other", entry_id="other_1").add_to_hass(hass)
    assert [entry.entry_id for entry in frigate_entries(hass)] == [FRIGATE_ENTRY_ID]


async def test_get_source(hass: HomeAssistant, fake_frigate: FakeFrigate) -> None:
    source = _source(hass)
    assert isinstance(source, FrigateSource)
    assert source.entry_id == FRIGATE_ENTRY_ID
    assert source.client is fake_frigate.client
    assert source.config is fake_frigate.config


async def test_get_source_missing_entry(hass: HomeAssistant) -> None:
    with pytest.raises(FrigateNotReady):
        async_get_frigate_source(hass, "nope")
    with pytest.raises(FrigateNotReady):
        async_get_frigate_source(hass, "")


async def test_get_source_wrong_domain(hass: HomeAssistant) -> None:
    MockConfigEntry(domain="other", entry_id="other_1").add_to_hass(hass)
    hass.data["frigate"] = {"other_1": {"client": object(), "config": {}}}
    with pytest.raises(FrigateNotReady):
        async_get_frigate_source(hass, "other_1")


@pytest.mark.parametrize(
    "runtime",
    [None, {}, {"config": {}}, {"client": object()}, {"client": None, "config": {}}, "bogus"],
)
async def test_get_source_runtime_data_not_ready(
    hass: HomeAssistant, fake_frigate: FakeFrigate, runtime: Any
) -> None:
    if runtime is None:
        hass.data["frigate"].pop(FRIGATE_ENTRY_ID)
    else:
        hass.data["frigate"][FRIGATE_ENTRY_ID] = runtime
    with pytest.raises(FrigateNotReady):
        async_get_frigate_source(hass, FRIGATE_ENTRY_ID)


async def test_get_source_no_frigate_domain_data(
    hass: HomeAssistant, fake_frigate: FakeFrigate
) -> None:
    hass.data.pop("frigate")
    with pytest.raises(FrigateNotReady):
        async_get_frigate_source(hass, FRIGATE_ENTRY_ID)


async def test_properties_read_live(hass: HomeAssistant, fake_frigate: FakeFrigate) -> None:
    source = _source(hass)
    new_config = {"cameras": {"b": {}, "a": {}}}
    _runtime(hass)["config"] = new_config
    assert source.config is new_config
    assert source.camera_names() == ["a", "b"]
    hass.data["frigate"].pop(FRIGATE_ENTRY_ID)
    assert source.config == {}
    assert source.camera_names() == []
    with pytest.raises(FrigateNotReady):
        source.client  # noqa: B018


async def test_base_url_and_ssl(hass: HomeAssistant, fake_frigate: FakeFrigate) -> None:
    source = _source(hass)
    assert source.base_url == FRIGATE_URL
    assert source.verify_ssl is True
    hass.config_entries.async_update_entry(
        fake_frigate.entry, data={"url": "https://frigate.local:8971//", "validate_ssl": False}
    )
    assert source.base_url == "https://frigate.local:8971"
    assert source.verify_ssl is False
    hass.config_entries.async_update_entry(fake_frigate.entry, data={"url": "http://x/"})
    assert source.base_url == "http://x"
    assert source.verify_ssl is True


async def test_base_url_entry_removed(hass: HomeAssistant, fake_frigate: FakeFrigate) -> None:
    source = _source(hass)
    await hass.config_entries.async_remove(FRIGATE_ENTRY_ID)
    with pytest.raises(FrigateNotReady):
        source.base_url  # noqa: B018


async def test_auth_headers(hass: HomeAssistant, fake_frigate: FakeFrigate) -> None:
    assert _source(hass).auth_headers() == {"Authorization": "Bearer test-token"}


async def test_auth_headers_failures(hass: HomeAssistant, fake_frigate: FakeFrigate) -> None:
    source = _source(hass)

    class Raising:
        def get_auth_headers(self) -> dict[str, str]:
            raise RuntimeError("boom")

    _runtime(hass)["client"] = Raising()
    assert source.auth_headers() == {}
    _runtime(hass)["client"] = object()
    assert source.auth_headers() == {}
    _runtime(hass).pop("client")
    assert source.auth_headers() == {}


async def test_camera_names(hass: HomeAssistant, fake_frigate: FakeFrigate) -> None:
    assert _source(hass).camera_names() == ["front_door", "garden_shed"]


@pytest.mark.parametrize(
    ("camera", "expected"),
    [
        ("tuin_huis", "Tuin Huis"),
        ("front_door", "Front Door"),
        ("garage", "Garage"),
        ("a__b_", "A B"),
    ],
)
async def test_camera_display_name(
    hass: HomeAssistant, fake_frigate: FakeFrigate, camera: str, expected: str
) -> None:
    assert _source(hass).camera_display_name(camera) == expected


async def test_pre_capture_seconds(hass: HomeAssistant, fake_frigate: FakeFrigate) -> None:
    source = _source(hass)
    assert source.pre_capture_seconds("front_door", "alert") == 5.0
    assert source.pre_capture_seconds("front_door", "detection") == 3.0
    assert source.pre_capture_seconds("front_door") == 3.0
    assert source.pre_capture_seconds("garden_shed", "alert") == 4.0
    assert source.pre_capture_seconds("garden_shed", None) == 2.0
    assert isinstance(source.pre_capture_seconds("front_door", "alert"), float)
    assert source.post_capture_seconds("front_door", "alert") == 8.0
    assert source.post_capture_seconds("front_door", "detection") == 3.0
    assert source.post_capture_seconds("unknown") == float(DEFAULT_PRE_CAPTURE_SECONDS)


async def test_pre_capture_seconds_fallbacks(
    hass: HomeAssistant, fake_frigate: FakeFrigate
) -> None:
    source = _source(hass)
    _runtime(hass)["config"] = {
        "cameras": {
            "only_alerts": {"record": {"alerts": {"pre_capture": 7}}},
            "only_detections": {"record": {"detections": {"pre_capture": 1.5}}},
            "zero": {"record": {"alerts": {"pre_capture": 0}, "detections": {"pre_capture": 9}}},
            "junk": {"record": {"alerts": {"pre_capture": "x"}, "detections": None}},
            "no_record": {},
        }
    }
    default = float(DEFAULT_PRE_CAPTURE_SECONDS)
    assert source.pre_capture_seconds("only_alerts", "detection") == 7.0
    assert source.pre_capture_seconds("only_detections", "alert") == 1.5
    assert source.pre_capture_seconds("zero", "alert") == 0.0
    assert source.pre_capture_seconds("junk", "alert") == default
    assert source.pre_capture_seconds("no_record") == default
    assert source.pre_capture_seconds("unknown", "alert") == default


async def test_source_entity_id(hass: HomeAssistant, fake_frigate: FakeFrigate) -> None:
    source = _source(hass)
    registry = er.async_get(hass)
    assert source.source_entity_id("front_door", "person") is None

    camera = registry.async_get_or_create(
        "camera",
        "frigate",
        f"{FRIGATE_ENTRY_ID}:camera:front_door",
        suggested_object_id="front_door",
    )
    assert source.source_entity_id("front_door", "person") == camera.entity_id

    sensor = registry.async_get_or_create(
        "binary_sensor",
        "frigate",
        f"{FRIGATE_ENTRY_ID}:occupancy_sensor:front_door_person",
        suggested_object_id="front_door_person_occupancy",
    )
    assert source.source_entity_id("front_door", "person") == sensor.entity_id
    assert source.source_entity_id("front_door", "dog") == camera.entity_id
    assert source.source_entity_id("garden_shed", "person") is None


async def test_source_entity_id_ignores_other_platforms_and_entries(
    hass: HomeAssistant, fake_frigate: FakeFrigate
) -> None:
    registry = er.async_get(hass)
    registry.async_get_or_create("camera", "other", f"{FRIGATE_ENTRY_ID}:camera:front_door")
    registry.async_get_or_create("camera", "frigate", "other_entry:camera:front_door")
    assert _source(hass).source_entity_id("front_door", "person") is None


async def test_async_get_events(hass: HomeAssistant, fake_frigate: FakeFrigate) -> None:
    source = _source(hass)
    after = time.time() - 24 * 3600 + 0.9
    events = await source.async_get_events(after=after, limit=50, labels=None)
    # The oldest fixture event is ~32h old and excluded.
    assert len(events) == len(fake_frigate.events) - 1
    starts = [event["start_time"] for event in events]
    assert starts == sorted(starts, reverse=True)
    name, kwargs = fake_frigate.client.calls[-1]
    assert name == "async_get_events"
    assert kwargs["after"] == int(after)
    assert isinstance(kwargs["after"], int)
    assert kwargs["limit"] == 50
    assert kwargs["labels"] is None


async def test_async_get_events_labels_and_limit(
    hass: HomeAssistant, fake_frigate: FakeFrigate
) -> None:
    source = _source(hass)
    events = await source.async_get_events(after=0, limit=2, labels=["person"])
    assert len(events) == 2
    assert {event["label"] for event in events} == {"person"}
    assert fake_frigate.client.calls[-1][1]["labels"] == ["person"]


async def test_async_get_events_sorts_defensively(
    hass: HomeAssistant, fake_frigate: FakeFrigate
) -> None:
    class Unsorted:
        async def async_get_events(self, **kwargs: Any) -> list[Any]:
            return [{"id": "a", "start_time": 1}, "junk", {"id": "c", "start_time": 3}, {"id": "b"}]

    _runtime(hass)["client"] = Unsorted()
    events = await _source(hass).async_get_events(after=0, limit=10, labels=None)
    assert [event["id"] for event in events] == ["c", "a", "b"]


async def test_async_get_events_non_list(hass: HomeAssistant, fake_frigate: FakeFrigate) -> None:
    class Weird:
        async def async_get_events(self, **kwargs: Any) -> Any:
            return {"error": "nope"}

    _runtime(hass)["client"] = Weird()
    assert await _source(hass).async_get_events(after=0, limit=10, labels=None) == []


async def test_async_get_events_error(hass: HomeAssistant, fake_frigate: FakeFrigate) -> None:
    source = _source(hass)
    fake_frigate.client.fail = True
    with pytest.raises(FrigateNotReady) as exc_info:
        await source.async_get_events(after=0, limit=10, labels=None)
    assert isinstance(exc_info.value.__cause__, FakeFrigateClientError)


async def test_async_get_events_client_gone(
    hass: HomeAssistant, fake_frigate: FakeFrigate
) -> None:
    source = _source(hass)
    hass.data["frigate"].pop(FRIGATE_ENTRY_ID)
    with pytest.raises(FrigateNotReady):
        await source.async_get_events(after=0, limit=10, labels=None)


async def test_async_get_event(hass: HomeAssistant, fake_frigate: FakeFrigate) -> None:
    source = _source(hass)
    wanted = fake_frigate.events[0]
    event = await source.async_get_event(wanted["id"])
    assert event == wanted
    assert fake_frigate.client.calls[-1] == ("async_get_event", {"event_id": wanted["id"]})
    assert await source.async_get_event("missing") is None


async def test_async_get_event_failures(hass: HomeAssistant, fake_frigate: FakeFrigate) -> None:
    source = _source(hass)
    fake_frigate.client.fail = True
    assert await source.async_get_event(fake_frigate.events[0]["id"]) is None

    class NonDict:
        async def async_get_event(self, event_id: str) -> Any:
            return ["not", "a", "dict"]

    _runtime(hass)["client"] = NonDict()
    assert await source.async_get_event("x") is None
    hass.data["frigate"].pop(FRIGATE_ENTRY_ID)
    assert await source.async_get_event("x") is None


async def test_async_get_reviews(hass: HomeAssistant, fake_frigate: FakeFrigate) -> None:
    source = async_get_frigate_source(hass, fake_frigate.entry.entry_id)
    reviews = await source.async_get_reviews(after=0, limit=3, labels=["person"])
    assert [review["data"]["objects"] for review in reviews] == [["person"]] * 3
    starts = [review["start_time"] for review in reviews]
    assert starts == sorted(starts, reverse=True)
    name, args = fake_frigate.client.calls[-1]
    assert name == "async_get_reviews"
    assert args["limit"] == 3 and args["after"] == 0

    fake_frigate.client.fail = True
    with pytest.raises(FrigateNotReady):
        await source.async_get_reviews(after=0, limit=3, labels=None)


async def test_async_get_review(hass: HomeAssistant, fake_frigate: FakeFrigate) -> None:
    source = async_get_frigate_source(hass, fake_frigate.entry.entry_id)
    review_id = fake_frigate.reviews[1]["id"]
    review = await source.async_get_review(review_id)
    assert review is not None and review["id"] == review_id
    assert fake_frigate.client.calls[-1] == (
        "api_wrapper",
        {"method": "get", "url": f"http://frigate.test:5000/api/review/{review_id}"},
    )
    assert await source.async_get_review("1.0-missing") is None
    fake_frigate.client.fail = True
    assert await source.async_get_review(review_id) is None


async def test_recording_enabled(hass: HomeAssistant, fake_frigate: FakeFrigate) -> None:
    source = async_get_frigate_source(hass, fake_frigate.entry.entry_id)
    assert source.recording_enabled("front_door")
    assert source.recording_enabled("unknown_camera")
    fake_frigate.config["cameras"]["front_door"]["record"]["enabled"] = False
    assert not source.recording_enabled("front_door")
