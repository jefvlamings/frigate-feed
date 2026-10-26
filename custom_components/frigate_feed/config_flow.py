"""Config and options flow for Frigate feed."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.core import callback
from homeassistant.data_entry_flow import SectionConfig, section
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import selector

from .const import (
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
    SUPPORTED_DETECTION_LABELS,
)
from .frigate_source import frigate_entries

ENTRY_TITLE = "Frigate Feed"
SECTION_RETENTION_POLICY = "retention_policy"

_LABEL_TITLES: dict[str, dict[str, str]] = {
    "en": {"person": "Person", "pet": "Pet", "vehicle": "Vehicle"},
    "nl": {"person": "Persoon", "pet": "Huisdier", "vehicle": "Voertuig"},
}


def _coerce_int(value: object, default: int, minimum: int, maximum: int) -> int:
    """Convert value to an int clamped to [minimum, maximum]; default on failure."""
    try:
        coerced = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError, OverflowError):
        coerced = default
    return max(minimum, min(maximum, coerced))


def _filter_labels(raw: object) -> list[str]:
    """Keep supported labels (in input order, deduplicated); fall back to defaults."""
    selected: list[str] = []
    if isinstance(raw, (list, tuple, set)):
        for label in raw:
            if label in SUPPORTED_DETECTION_LABELS and label not in selected:
                selected.append(label)
    return selected or list(DEFAULT_ENABLED_DETECTION_LABELS)


def _frigate_entry_label(entry: ConfigEntry) -> str:
    """Human readable label for a Frigate config entry (title + url)."""
    title = entry.title or entry.entry_id
    url = str(entry.data.get("url") or "").strip()
    return f"{title} ({url})" if url else title


class FrigateFeedConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle config flow for Frigate feed."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Bind the single instance to a Frigate config entry."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        entries = frigate_entries(self.hass)
        if not entries:
            return self.async_abort(reason="no_frigate")
        if len(entries) == 1:
            return self._create(entries[0].entry_id)

        errors: dict[str, str] = {}
        if user_input is not None:
            selected = str(user_input.get(CONF_FRIGATE_ENTRY_ID, ""))
            if selected in {entry.entry_id for entry in entries}:
                return self._create(selected)
            errors["base"] = "unknown_frigate_entry"

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_FRIGATE_ENTRY_ID, default=entries[0].entry_id
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(
                                    value=entry.entry_id, label=_frigate_entry_label(entry)
                                )
                                for entry in entries
                            ],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
            errors=errors,
        )

    def _create(self, frigate_entry_id: str) -> ConfigFlowResult:
        return self.async_create_entry(
            title=ENTRY_TITLE, data={CONF_FRIGATE_ENTRY_ID: frigate_entry_id}
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> FrigateFeedOptionsFlow:
        """Get options flow."""
        return FrigateFeedOptionsFlow()


class FrigateFeedOptionsFlow(config_entries.OptionsFlow):
    """Handle options flow for Frigate feed."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage options."""
        if user_input is not None:
            merged = dict(user_input)
            section_input = merged.pop(SECTION_RETENTION_POLICY, None)
            if isinstance(section_input, dict):
                merged.update(section_input)
            return self.async_create_entry(title="", data=self._normalize(merged))

        current = self._normalize(dict(self.config_entry.options))
        retention_schema = {
            vol.Required(
                CONF_RETENTION_HOURS, default=current[CONF_RETENTION_HOURS]
            ): _number_selector(MIN_RETENTION_HOURS, MAX_RETENTION_HOURS),
            vol.Required(
                CONF_MAX_DETECTIONS, default=current[CONF_MAX_DETECTIONS]
            ): _number_selector(MIN_MAX_DETECTIONS, MAX_MAX_DETECTIONS),
        }
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ENABLED_LABELS, default=current[CONF_ENABLED_LABELS]
                    ): cv.multi_select(self._label_options()),
                    vol.Required(SECTION_RETENTION_POLICY): section(
                        vol.Schema(retention_schema),
                        SectionConfig({"collapsed": False}),
                    ),
                }
            ),
        )

    @staticmethod
    def _normalize(raw: dict[str, Any]) -> dict[str, Any]:
        """Return sanitized options (supported labels, clamped numbers)."""
        return {
            CONF_ENABLED_LABELS: _filter_labels(raw.get(CONF_ENABLED_LABELS)),
            CONF_RETENTION_HOURS: _coerce_int(
                raw.get(CONF_RETENTION_HOURS, DEFAULT_RETENTION_HOURS),
                DEFAULT_RETENTION_HOURS,
                MIN_RETENTION_HOURS,
                MAX_RETENTION_HOURS,
            ),
            CONF_MAX_DETECTIONS: _coerce_int(
                raw.get(CONF_MAX_DETECTIONS, DEFAULT_MAX_DETECTIONS),
                DEFAULT_MAX_DETECTIONS,
                MIN_MAX_DETECTIONS,
                MAX_MAX_DETECTIONS,
            ),
        }

    def _label_options(self) -> dict[str, str]:
        language = str(self.hass.config.language or "en").lower()
        titles = _LABEL_TITLES.get(language.split("-")[0], _LABEL_TITLES["en"])
        return {label: titles.get(label, label.title()) for label in SUPPORTED_DETECTION_LABELS}


def _number_selector(minimum: int, maximum: int) -> selector.NumberSelector:
    return selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=minimum, max=maximum, step=1, mode=selector.NumberSelectorMode.BOX
        )
    )
