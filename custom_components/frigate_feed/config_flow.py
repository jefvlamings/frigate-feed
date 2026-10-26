"""Config flow for Frigate feed (minimal scaffold; full flow in a follow-up)."""

from __future__ import annotations

from homeassistant import config_entries

from .const import CONF_FRIGATE_ENTRY_ID, DOMAIN
from .frigate_source import frigate_entries


class FrigateFeedConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle config flow for Frigate feed."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Create a single instance bound to the first Frigate entry."""
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        entries = frigate_entries(self.hass)
        if not entries:
            return self.async_abort(reason="no_frigate")
        return self.async_create_entry(
            title="Frigate Feed", data={CONF_FRIGATE_ENTRY_ID: entries[0].entry_id}
        )
