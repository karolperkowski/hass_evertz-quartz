"""Diagnostics support for Evertz Quartz."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .quartz_client import QuartzClient


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """
    Return diagnostics data for a config entry.

    This populates the "Download diagnostics" button in
    Settings → Devices & Services → Evertz Quartz Router.
    """
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    client: QuartzClient | None = data.get("client")

    return {
        "config_entry": {
            "entry_id": entry.entry_id,
            "title": entry.title,
            "data": {
                # Redact nothing here — no passwords/tokens in this integration
                **entry.data,
            },
            "options": dict(entry.options),
        },
        "client": client.get_diagnostics() if client else {"error": "client not initialised"},
    }
