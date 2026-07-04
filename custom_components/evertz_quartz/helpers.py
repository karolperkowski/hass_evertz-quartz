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


def notify_blocked_route(
    hass,
    entry: ConfigEntry,
    client,
    *,
    reason: str,
    dest_order: int,
    src_order: int | None = None,
    user_id: str | None = None,
    origin: str = "select",
    action: str = "route",
) -> None:
    """Surface a blocked operation to the user, identically from every path.

    Fires ``evertz_quartz_route_blocked`` on the event bus (for automations —
    mobile push, logging, etc.) and raises a persistent notification.
    Enforcement paths: the destination select entity (origin="select"), the
    evertz_quartz.route service (origin="service"), and the destination lock
    entity (origin="lock").

    reason: "read_only" | "locked" | "cross_namespace"
    action: "route" | "lock" | "unlock" — what was attempted and blocked
    """
    from .const import EVENT_ROUTE_BLOCKED

    rname     = router_display_name(entry)
    dest_name = client.destination_names.get(dest_order) or f"Destination {dest_order}"
    src_name  = (
        (client.source_names.get(src_order) or f"Source {src_order}")
        if src_order else None
    )
    dest_ns = client.destination_namespaces.get(dest_order)
    src_ns  = client.source_namespaces.get(src_order) if src_order else None

    hass.bus.async_fire(EVENT_ROUTE_BLOCKED, {
        "router":           rname,
        "entry_id":         entry.entry_id,
        "reason":           reason,
        "origin":           origin,
        "action":           action,
        "destination":      dest_order,
        "destination_name": dest_name,
        "source":           src_order,
        "source_name":      src_name,
        "user_id":          user_id,
    })

    if reason == "read_only":
        notif_id = f"evertz_quartz_{entry.entry_id}_readonly_{dest_order}"
        blocked_for = "your user account" if user_id else "automations and scripts"
        if action == "route":
            title_action = "Route"
            blocked_what = "Routes to this destination are"
        else:  # "lock" / "unlock"
            title_action = action.capitalize()
            blocked_what = f"{action.capitalize()}ing this destination is"
        title   = f"Evertz Quartz [{rname}] — {title_action} Blocked: Read-Only Destination"
        message = (
            f"**{dest_name}** is marked **read-only**.\n\n"
            f"{blocked_what} blocked for {blocked_for}.\n\n"
            "An administrator can change this under "
            "**Settings → Devices & Services → Evertz Quartz → Configure**."
        )
    elif reason == "locked":
        notif_id = f"evertz_quartz_{entry.entry_id}_locked_{dest_order}"
        title    = f"Evertz Quartz [{rname}] — Route Blocked: Destination Locked"
        message  = (
            f"**{dest_name}** is currently **locked**.\n\n"
            "Routes to this destination are blocked until it is unlocked.\n\n"
            f"Use the **{dest_name} Lock** entity on the device card to unlock it."
        )
    else:  # cross_namespace
        notif_id = f"evertz_quartz_{entry.entry_id}_cross_namespace"
        title    = f"Evertz Quartz [{rname}] — Cross-Namespace Route Blocked"
        message  = (
            f"**Route blocked:** `{src_name}` (namespace: **{src_ns}**)"
            f" → `{dest_name}` (namespace: **{dest_ns}**)\n\n"
            "These sources and destinations belong to different physical "
            "routers and cannot be cross-routed.\n\n"
            f"Only **{dest_ns}** sources are valid for `{dest_name}`."
        )

    hass.async_create_task(
        hass.services.async_call("persistent_notification", "create", {
            "notification_id": notif_id,
            "title": title,
            "message": message,
        })
    )


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

    # Read from manifest.json directly — most reliable for custom components
    try:
        import json
        import pathlib
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
