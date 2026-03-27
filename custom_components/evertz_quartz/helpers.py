"""Shared helpers for the Evertz Quartz integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry


def effective(entry: ConfigEntry, key: str, default):
    """Return options value if set, otherwise fall back to data, then default.

    Options always take priority — this lets the options flow override any
    value originally set during config flow without touching entry.data.
    """
    if key in entry.options:
        return entry.options[key]
    return entry.data.get(key, default)


def router_display_name(entry: ConfigEntry) -> str:
    """Human-readable router name: CONF_NAME → host IP → fallback."""
    from .const import CONF_HOST, CONF_NAME
    return entry.data.get(CONF_NAME) or entry.data.get(CONF_HOST, "Unknown Router")
