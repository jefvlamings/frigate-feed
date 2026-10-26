"""Tests for the Frigate Feed config and options flow."""

from __future__ import annotations

from collections.abc import Iterator
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, section

from custom_components.frigate_feed.config_flow import SECTION_RETENTION_POLICY
from custom_components.frigate_feed.const import (
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
)

from .conftest import FRIGATE_ENTRY_ID, FakeFrigate

COMPONENT_DIR = Path(__file__).parent.parent / "custom_components" / DOMAIN
TRANSLATION_FILES = (
    COMPONENT_DIR / "strings.json",
    COMPONENT_DIR / "translations" / "en.json",
    COMPONENT_DIR / "translations" / "nl.json",
)


@pytest.fixture(autouse=True)
def mock_setup_entry() -> Iterator[None]:
    """Skip runtime setup; other units own those modules."""
    with patch("custom_components.frigate_feed.async_setup_entry", return_value=True):
        yield


def _add_frigate_entry(hass: HomeAssistant, entry_id: str, title: str, url: str) -> MockConfigEntry:
    entry = MockConfigEntry(domain="frigate", entry_id=entry_id, title=title, data={"url": url})
    entry.add_to_hass(hass)
    return entry


def _add_feed_entry(hass: HomeAssistant, options: dict[str, Any] | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Frigate Feed",
        unique_id=DOMAIN,
        data={CONF_FRIGATE_ENTRY_ID: FRIGATE_ENTRY_ID},
        options=options or {},
    )
    entry.add_to_hass(hass)
    return entry


async def _start_user_flow(hass: HomeAssistant) -> dict[str, Any]:
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def test_user_flow_aborts_without_frigate(hass: HomeAssistant) -> None:
    result = await _start_user_flow(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_frigate"


async def test_user_flow_single_frigate_creates_entry(
    hass: HomeAssistant, fake_frigate: FakeFrigate
) -> None:
    result = await _start_user_flow(hass)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Frigate Feed"
    assert result["data"] == {CONF_FRIGATE_ENTRY_ID: FRIGATE_ENTRY_ID}
    entry = result["result"]
    assert entry.unique_id == DOMAIN


async def test_user_flow_several_frigate_entries_shows_selection(
    hass: HomeAssistant, fake_frigate: FakeFrigate
) -> None:
    _add_frigate_entry(hass, "frigate_entry_2", "Frigate Garage", "http://garage.test:5000")

    result = await _start_user_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    schema = result["data_schema"].schema
    key = next(iter(schema))
    assert str(key) == CONF_FRIGATE_ENTRY_ID
    options = schema[key].config["options"]
    assert {option["value"] for option in options} == {FRIGATE_ENTRY_ID, "frigate_entry_2"}
    labels = {option["value"]: option["label"] for option in options}
    assert labels["frigate_entry_2"] == "Frigate Garage (http://garage.test:5000)"
    assert labels[FRIGATE_ENTRY_ID] == "Frigate (http://frigate.test:5000)"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_FRIGATE_ENTRY_ID: "frigate_entry_2"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_FRIGATE_ENTRY_ID: "frigate_entry_2"}


async def test_user_flow_selected_entry_removed_shows_error(
    hass: HomeAssistant, fake_frigate: FakeFrigate
) -> None:
    _add_frigate_entry(hass, "frigate_entry_2", "Frigate Garage", "http://garage.test:5000")
    _add_frigate_entry(hass, "frigate_entry_3", "Frigate Tuin", "http://tuin.test:5000")
    result = await _start_user_flow(hass)
    await hass.config_entries.async_remove("frigate_entry_3")

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_FRIGATE_ENTRY_ID: "frigate_entry_3"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "unknown_frigate_entry"}


async def test_user_flow_single_instance(hass: HomeAssistant, fake_frigate: FakeFrigate) -> None:
    _add_feed_entry(hass)
    result = await _start_user_flow(hass)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


def _form_defaults(result: dict[str, Any]) -> dict[str, Any]:
    defaults: dict[str, Any] = {}
    for key, value in result["data_schema"].schema.items():
        if isinstance(value, section):
            for inner_key in value.schema.schema:
                defaults[str(inner_key)] = inner_key.default()
        else:
            defaults[str(key)] = key.default()
    return defaults


async def test_options_flow_defaults(hass: HomeAssistant) -> None:
    entry = _add_feed_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert _form_defaults(result) == {
        CONF_ENABLED_LABELS: list(DEFAULT_ENABLED_DETECTION_LABELS),
        CONF_RETENTION_HOURS: DEFAULT_RETENTION_HOURS,
        CONF_MAX_DETECTIONS: DEFAULT_MAX_DETECTIONS,
    }


async def test_options_flow_uses_existing_options(hass: HomeAssistant) -> None:
    entry = _add_feed_entry(
        hass,
        {CONF_ENABLED_LABELS: ["vehicle", "motion"], CONF_RETENTION_HOURS: 48, CONF_MAX_DETECTIONS: 500},
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert _form_defaults(result) == {
        CONF_ENABLED_LABELS: ["vehicle"],
        CONF_RETENTION_HOURS: 48,
        CONF_MAX_DETECTIONS: 500,
    }


async def test_options_flow_saves(hass: HomeAssistant) -> None:
    entry = _add_feed_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_ENABLED_LABELS: ["person", "pet"],
            SECTION_RETENTION_POLICY: {CONF_RETENTION_HOURS: 12.0, CONF_MAX_DETECTIONS: 250.0},
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    expected = {
        CONF_ENABLED_LABELS: ["person", "pet"],
        CONF_RETENTION_HOURS: 12,
        CONF_MAX_DETECTIONS: 250,
    }
    assert result["data"] == expected
    assert dict(entry.options) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            {CONF_RETENTION_HOURS: 0, CONF_MAX_DETECTIONS: 1},
            {CONF_RETENTION_HOURS: MIN_RETENTION_HOURS, CONF_MAX_DETECTIONS: MIN_MAX_DETECTIONS},
        ),
        (
            {CONF_RETENTION_HOURS: 10_000, CONF_MAX_DETECTIONS: 1_000_000},
            {CONF_RETENTION_HOURS: MAX_RETENTION_HOURS, CONF_MAX_DETECTIONS: MAX_MAX_DETECTIONS},
        ),
    ],
)
async def test_options_flow_clamps(
    hass: HomeAssistant, raw: dict[str, int], expected: dict[str, int]
) -> None:
    entry = _add_feed_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    flow = hass.config_entries.options._progress[result["flow_id"]]
    # Bypass schema validation (the selectors would reject out-of-range values) to
    # exercise the flow's own clamping.
    result = await flow.async_step_init({CONF_ENABLED_LABELS: ["person"], SECTION_RETENTION_POLICY: raw})
    assert result["data"] == {CONF_ENABLED_LABELS: ["person"], **expected}


async def test_options_flow_clamps_stored_garbage(hass: HomeAssistant) -> None:
    entry = _add_feed_entry(
        hass, {CONF_RETENTION_HOURS: "abc", CONF_MAX_DETECTIONS: 99_999, CONF_ENABLED_LABELS: "person"}
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert _form_defaults(result) == {
        CONF_ENABLED_LABELS: list(DEFAULT_ENABLED_DETECTION_LABELS),
        CONF_RETENTION_HOURS: DEFAULT_RETENTION_HOURS,
        CONF_MAX_DETECTIONS: MAX_MAX_DETECTIONS,
    }


async def test_options_flow_empty_labels_fall_back_to_defaults(hass: HomeAssistant) -> None:
    entry = _add_feed_entry(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_ENABLED_LABELS: [],
            SECTION_RETENTION_POLICY: {CONF_RETENTION_HOURS: 24, CONF_MAX_DETECTIONS: 100},
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ENABLED_LABELS] == list(DEFAULT_ENABLED_DETECTION_LABELS)


async def test_options_label_titles_follow_language(hass: HomeAssistant) -> None:
    entry = _add_feed_entry(hass)
    hass.config.language = "nl"
    result = await hass.config_entries.options.async_init(entry.entry_id)
    key = next(key for key in result["data_schema"].schema if str(key) == CONF_ENABLED_LABELS)
    assert result["data_schema"].schema[key].options == {
        "person": "Persoon",
        "pet": "Huisdier",
        "vehicle": "Voertuig",
    }


# --- translation coverage -------------------------------------------------


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", TRANSLATION_FILES, ids=lambda path: path.name)
def test_translation_files_parse(path: Path) -> None:
    assert isinstance(_load(path), dict)


def test_translation_files_share_keys() -> None:
    def flatten(data: Any, prefix: str = "") -> set[str]:
        if not isinstance(data, dict):
            return {prefix}
        keys: set[str] = set()
        for key, value in data.items():
            keys |= flatten(value, f"{prefix}.{key}" if prefix else key)
        return keys

    reference = flatten(_load(TRANSLATION_FILES[0]))
    for path in TRANSLATION_FILES[1:]:
        assert flatten(_load(path)) == reference, path.name


def _assert_form_translated(result: dict[str, Any], step: dict[str, Any]) -> None:
    for key, value in result["data_schema"].schema.items():
        if isinstance(value, section):
            section_strings = step["sections"][str(key)]
            assert section_strings["name"]
            for inner_key in value.schema.schema:
                assert section_strings["data"][str(inner_key)]
        else:
            assert step["data"][str(key)]


async def test_forms_and_aborts_have_strings(
    hass: HomeAssistant, fake_frigate: FakeFrigate
) -> None:
    _add_frigate_entry(hass, "frigate_entry_2", "Frigate Garage", "http://garage.test:5000")
    user_form = await _start_user_flow(hass)
    hass.config_entries.flow.async_abort(user_form["flow_id"])
    entry = _add_feed_entry(hass)
    options_form = await hass.config_entries.options.async_init(entry.entry_id)
    abort_reasons = ("single_instance_allowed", "no_frigate", "already_configured")

    for path in TRANSLATION_FILES:
        strings = _load(path)
        assert strings["title"]
        user_step = strings["config"]["step"][user_form["step_id"]]
        assert user_step["title"]
        _assert_form_translated(user_form, user_step)
        _assert_form_translated(options_form, strings["options"]["step"][options_form["step_id"]])
        assert strings["config"]["error"]["unknown_frigate_entry"]
        for reason in abort_reasons:
            assert strings["config"]["abort"][reason], (path.name, reason)
