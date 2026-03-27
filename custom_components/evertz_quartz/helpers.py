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


def device_info(entry: ConfigEntry):
    """
    Shared DeviceInfo for all platforms.

    Fields visible on the HA device page:
      Name          — router display name (CONF_NAME or host IP)
      Manufacturer  — Evertz
      Model         — EQX / EQT / MAGNUM  (+ host:port)
      Firmware      — sw_version: integration version
      Hardware      — hw_version: profile dimensions + CSV status
      Config URL    — links to http://host:port
    """
    from homeassistant.helpers.entity import DeviceInfo
    from .const import (
        CONF_HOST, CONF_MAX_SOURCES, CONF_MAX_DESTINATIONS,
        CONF_CSV_LOADED, DOMAIN,
    )
    from .helpers import effective, router_display_name
    from importlib.metadata import version as pkg_version

    # Integration version from manifest
    try:
        from homeassistant.loader import async_get_custom_components
        integ_version = entry.data.get("_version", "")
    except Exception:  # noqa: BLE001
        integ_version = ""

    # Read from manifest.json directly — most reliable for custom components
    try:
        import json, pathlib
        manifest_path = pathlib.Path(__file__).parent / "manifest.json"
        integ_version = json.loads(manifest_path.read_text())["version"]
    except Exception:  # noqa: BLE001
        integ_version = "unknown"

    host     = entry.data.get(CONF_HOST, "")
    port     = entry.data.get("port", 6666)
    max_src  = entry.data.get(CONF_MAX_SOURCES, "?")
    max_dst  = entry.data.get(CONF_MAX_DESTINATIONS, "?")
    csv_flag = entry.data.get(CONF_CSV_LOADED, False)
    csv_label = "CSV profile loaded" if csv_flag else "No CSV"

    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=router_display_name(entry),
        manufacturer="Evertz",
        model=f"EQX / EQT / MAGNUM — {host}:{port}",
        sw_version=integ_version,
        hw_version=f"{max_src} src × {max_dst} dst — {csv_label}",
        configuration_url=f"http://{host}:{port}",
    )
