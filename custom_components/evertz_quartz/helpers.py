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


async def apply_resize(hass, entry: ConfigEntry, new_src: int, new_dst: int) -> bool:
    """Persist a new matrix size to entry.data and reload to rebuild entities.

    Shared by the options-flow profile step and the "Resize to Detected" button
    so there is a single path for changing the configured size. Counts live in
    entry.data (never entry.options), so CSV names already stored for slots
    within the new range survive the reload. Returns True when a change was
    applied, False when the requested size already matched.
    """
    from .const import (
        CONF_MAX_DESTINATIONS,
        CONF_MAX_SOURCES,
        DEFAULT_MAX_DESTINATIONS,
        DEFAULT_MAX_SOURCES,
    )

    cur_src = effective(entry, CONF_MAX_SOURCES, DEFAULT_MAX_SOURCES)
    cur_dst = effective(entry, CONF_MAX_DESTINATIONS, DEFAULT_MAX_DESTINATIONS)
    if new_src == cur_src and new_dst == cur_dst:
        return False

    new_data = dict(entry.data)
    new_data[CONF_MAX_SOURCES] = new_src
    new_data[CONF_MAX_DESTINATIONS] = new_dst
    hass.config_entries.async_update_entry(entry, data=new_data)
    hass.async_create_task(hass.config_entries.async_reload(entry.entry_id))
    return True


def readonly_destinations(entry: ConfigEntry) -> set[int]:
    """Destination Orders marked read-only in the Configure panel."""
    from .const import CONF_READONLY_DESTINATIONS
    return {int(o) for o in entry.options.get(CONF_READONLY_DESTINATIONS, [])}


def user_can_route(entry: ConfigEntry, dest_order: int, user_id: str | None) -> bool:
    """Return True if a take to this destination is permitted for this user.

    Destinations not marked read-only are open to everyone. Read-only
    destinations accept takes only from HA users in the allowed list.
    Calls without a user context (automations, scripts) are blocked on
    read-only destinations.
    """
    from .const import CONF_READONLY_ALLOWED_USERS
    if dest_order not in readonly_destinations(entry):
        return True
    allowed = entry.options.get(CONF_READONLY_ALLOWED_USERS, [])
    return bool(user_id) and user_id in allowed


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
        configuration_url=f"http://{host}",
    )
