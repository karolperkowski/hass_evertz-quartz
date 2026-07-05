"""Evertz Quartz Router integration for Home Assistant."""

from __future__ import annotations

import asyncio
import logging
import time

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady, ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr

from .const import (
    CONF_CONNECT_TIMEOUT,
    CONF_CSV_LOADED,
    CONF_LEVELS,
    CONF_MAX_DESTINATIONS,
    CONF_MAX_SOURCES,
    CONF_RECONNECT_DELAY,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_LEVELS,
    DEFAULT_MAX_DESTINATIONS,
    DEFAULT_MAX_SOURCES,
    DEFAULT_RECONNECT_DELAY,
    DOMAIN,
)
from .helpers import (
    detection_status,
    effective,
    notify_blocked_route,
    router_display_name,
    user_can_route,
)
from .quartz_client import QuartzClient

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SELECT, Platform.BUTTON, Platform.BINARY_SENSOR, Platform.SENSOR, Platform.LOCK]

ATTR_DESTINATION  = "destination"
ATTR_SOURCE       = "source"
ATTR_LEVELS       = "levels"
ATTR_DEVICE_ID    = "device_id"
ATTR_ROUTER_NAME  = "router_name"



async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Evertz Quartz from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    route_listeners: list = []
    mnemonic_listeners: list = []
    mismatch_listeners: list = []
    connection_listeners: list = []   # notified on connect/disconnect
    lock_listeners: list = []         # notified on .BA lock state change
    mismatch_orders: set = set()   # (kind, order) pairs seen out-of-range this session

    def _route_callback(dest: int, src: int, levels: str) -> None:
        for cb in route_listeners:
            hass.loop.call_soon_threadsafe(cb, dest, src, levels)

    def _mnemonic_callback() -> None:
        for cb in mnemonic_listeners:
            hass.loop.call_soon_threadsafe(cb)

    async def _detection_check() -> None:
        """Compare the detected destination count to the configured size and
        warn if the profile is over-provisioned.

        Runs after the connect-time sync sweep completes (driven by
        sync_callback, not a wall-clock guess): the .I interrogation makes the
        controller answer .A for real destinations and .E for ones that don't
        exist, so client.max_dst_order_seen settles at the true count.
        Detection only — the user applies any change via Configure → Update
        Profile. Skips silently when the controller ignores .I (detected
        count stays 0).
        """
        data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
        client = data.get("client")
        if not client or not client.connected:
            return
        status = detection_status(entry, client)
        if not status.over_provisioned:
            return
        detected = status.detected_destinations
        configured = status.configured_destinations
        rname = router_display_name(entry)
        _LOGGER.info(
            "[%s] Over-provisioned: detected %d destination(s), configured %d",
            rname, detected, configured,
        )
        hass.async_create_task(
            hass.services.async_call(
                "persistent_notification", "create", {
                    "notification_id": f"evertz_quartz_{entry.entry_id}_overprovision",
                    "title": f"Evertz Quartz [{rname}] — Fewer Destinations Than Configured",
                    "message": (
                        f"The controller answered interrogation for **{detected}** "
                        f"destination(s), but this integration is configured for "
                        f"**{configured}**. The extra destinations are unused placeholders.\n\n"
                        "To match the controller, open **Configure → Next → Update "
                        f"Profile** and set **Max Destinations = {detected}** "
                        "(or upload the profile CSV)."
                    ),
                }
            )
        )
        for cb in mismatch_listeners:
            hass.loop.call_soon_threadsafe(cb)

    # ── Startup sync notification ─────────────────────────────────────────
    # After a restart/reload the destination selects show "Unknown" until the
    # connect-time interrogation sweep (.I routes, .BI locks, .RD/.RT names)
    # completes. Tell the user that's expected — once, on the first connect —
    # and clear the message automatically when the sweep finishes.
    sync_notif_id = f"evertz_quartz_{entry.entry_id}_startup_sync"
    sync_state: dict = {"notified": False, "t0": None, "finish_task": None}

    def _cancel_finish_task() -> None:
        task: asyncio.Task | None = sync_state.get("finish_task")
        if task and not task.done():
            task.cancel()
        sync_state["finish_task"] = None

    def _dismiss_sync_notification() -> None:
        hass.async_create_task(
            hass.services.async_call(
                "persistent_notification", "dismiss",
                {"notification_id": sync_notif_id},
            )
        )

    async def _announce_sync() -> None:
        client = hass.data.get(DOMAIN, {}).get(entry.entry_id, {}).get("client")
        if not client:
            return
        rname = router_display_name(entry)
        est = client.estimated_sync_seconds()
        names_part = (
            "" if client.csv_loaded
            else ", and querying source/destination names"
        )
        await hass.services.async_call("persistent_notification", "create", {
            "notification_id": sync_notif_id,
            "title": f"Evertz Quartz [{rname}] — Synchronizing",
            "message": (
                f"Connected to the router — now synchronizing **current routes "
                f"and lock states**{names_part}.\n\n"
                f"Destination entities may show **Unknown** until this completes "
                f"(roughly **~{est} seconds** for this profile size).\n\n"
                "This notification clears automatically when the sync finishes."
            ),
        })

    def _sync_complete_callback() -> None:
        """Called by the client when the connect-time sync sweep has been sent."""
        async def _finish() -> None:
            await asyncio.sleep(2)  # grace for the last replies to drain
            _dismiss_sync_notification()
            client = hass.data.get(DOMAIN, {}).get(entry.entry_id, {}).get("client")
            t0 = sync_state.get("t0")
            elapsed = f" in {time.monotonic() - t0:.1f}s" if t0 else ""
            if client:
                _LOGGER.info(
                    "[%s] Startup sync complete%s — %d routes, %d lock states known",
                    router_display_name(entry), elapsed,
                    len(client.routes), len(client.locks),
                )
            # Interrogation replies have drained — check for over-provisioning
            await _detection_check()

        def _schedule() -> None:
            _cancel_finish_task()  # reconnect while a grace task is pending
            sync_state["finish_task"] = hass.async_create_task(_finish())

        hass.loop.call_soon_threadsafe(_schedule)

    def _connection_callback(connected: bool) -> None:
        name = router_display_name(entry)
        _LOGGER.info("Evertz Quartz [%s] %s", name, "connected" if connected else "disconnected")
        for cb in connection_listeners:
            hass.loop.call_soon_threadsafe(cb)
        if connected:
            if not sync_state["notified"]:
                sync_state["notified"] = True
                sync_state["t0"] = time.monotonic()
                hass.loop.call_soon_threadsafe(lambda: hass.async_create_task(_announce_sync()))
        else:
            # Connection dropped — a lingering "synchronizing" message would lie
            hass.loop.call_soon_threadsafe(_dismiss_sync_notification)
            hass.loop.call_soon_threadsafe(_cancel_finish_task)

    def _lock_callback(dest_order: int, lock_value: int) -> None:
        """Called by client when a .BA lock state message is received."""
        for cb in lock_listeners:
            hass.loop.call_soon_threadsafe(cb, dest_order, lock_value)

    def _notify_callback(kind: str, order: int) -> None:
        """Called by client when an out-of-range Order is received."""
        key = (kind, order)
        mismatch_orders.add(key)
        label = "source" if kind == "src" else "destination"
        limit_key = "max_sources" if kind == "src" else "max_destinations"
        limit = entry.data.get(limit_key, 0)
        rname = router_display_name(entry)
        # Fire HA persistent notification
        hass.async_create_task(
            hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "notification_id": f"evertz_quartz_{entry.entry_id}_profile_mismatch",
                    "title": f"Evertz Quartz [{rname}] — Profile Mismatch",
                    "message": (
                        f"**Router:** {rname}\n\n"
                        f"The router reported {label} Order **{order}**, which is outside "
                        f"the configured range (current maximum: **{limit}**). "
                        f"The router profile has likely expanded or changed.\n\n"
                        "**To fix this:**\n"
                        "1. Click [**Open Configure \u2192**]"
                        "(/config/integrations/integration/evertz_quartz) "
                        f"to go to the Evertz Quartz integrations page\n"
                        f"2. Find **{rname}** and click the \u2699\ufe0f **gear icon** (Configure)\n"
                        "3. The *Connection Settings* page opens — click **Next** "
                        "(no changes needed on this page)\n"
                        "4. On the *Update Profile* page, either:\n"
                        "   - Upload a new `profile_availability.csv`, **or**\n"
                        "   - Increase **Max Sources** / **Max Destinations** manually\n"
                        "5. Click **Submit** — the integration will reload automatically\n\n"
                        "\u2139\ufe0f The profile mismatch sensor will clear once the reload completes."
                    ),
                },
            )
        )
        # Notify binary sensor
        for cb in mismatch_listeners:
            hass.loop.call_soon_threadsafe(cb)

    # Load port maps (Order → Quartz port) — persisted from CSV
    src_port_map = {int(k): v for k, v in entry.data.get("source_port_map", {}).items()}
    dst_port_map = {int(k): v for k, v in entry.data.get("destination_port_map", {}).items()}
    csv_loaded   = entry.data.get(CONF_CSV_LOADED, False)

    rname = router_display_name(entry)

    if src_port_map:
        _LOGGER.debug("[%s] Loaded source port map (%d entries)", rname, len(src_port_map))
    if dst_port_map:
        _LOGGER.debug("[%s] Loaded destination port map (%d entries)", rname, len(dst_port_map))
    client = QuartzClient(
        host=entry.data[CONF_HOST],
        port=entry.data[CONF_PORT],
        max_sources=effective(entry, CONF_MAX_SOURCES,      DEFAULT_MAX_SOURCES),
        max_destinations=effective(entry, CONF_MAX_DESTINATIONS, DEFAULT_MAX_DESTINATIONS),
        levels=effective(entry, CONF_LEVELS,            DEFAULT_LEVELS),
        router_name=rname,
        csv_loaded=csv_loaded,
        route_callback=_route_callback,
        mnemonic_callback=_mnemonic_callback,
        connection_callback=_connection_callback,
        notify_callback=_notify_callback,
        lock_callback=_lock_callback,
        sync_callback=_sync_complete_callback,
        reconnect_delay=effective(entry, CONF_RECONNECT_DELAY,  DEFAULT_RECONNECT_DELAY),
        connect_timeout=effective(entry, CONF_CONNECT_TIMEOUT,  DEFAULT_CONNECT_TIMEOUT),
    )
    # Store port maps in client for diagnostics/reference — not used in protocol commands
    client.src_port_map = src_port_map
    client.dst_port_map = dst_port_map

    # Load names from entry.data — only when CSV is loaded
    # Names are keyed by Order (MAGNUM numbering), not Quartz Port Number
    if csv_loaded:
        if stored_src := entry.data.get("source_names"):
            client.source_names.update({int(k): v for k, v in stored_src.items()})
            _LOGGER.debug("[%s] Loaded %d source names from CSV profile", rname, len(stored_src))
        if stored_dst := entry.data.get("destination_names"):
            client.destination_names.update({int(k): v for k, v in stored_dst.items()})
            _LOGGER.debug("[%s] Loaded %d destination names from CSV profile", rname, len(stored_dst))
        if stored_src_ns := entry.data.get("source_namespaces"):
            client.source_namespaces.update({int(k): v for k, v in stored_src_ns.items()})
            unique_ns = set(stored_src_ns.values())
            _LOGGER.debug("[%s] Loaded %d source namespaces: %s", rname, len(stored_src_ns), sorted(unique_ns))
        if stored_dst_ns := entry.data.get("destination_namespaces"):
            client.destination_namespaces.update({int(k): v for k, v in stored_dst_ns.items()})
            if not entry.data.get("source_namespaces"):
                _LOGGER.warning(
                    "[%s] Destination namespaces loaded but no source namespaces — "
                    "namespace filtering will be partially disabled", rname
                )
        if not stored_src_ns and client.source_names:
            _LOGGER.info(
                "[%s] No namespace data in CSV — namespace filtering disabled. "
                "Re-import CSV to enable cross-namespace route blocking.", rname
            )
        # Hidden rows stay in the name/port maps (MAGNUM still uses their
        # Orders) but are excluded from the source dropdown options.
        client.hidden_sources = {int(o) for o in entry.data.get("hidden_source_orders", [])}
        client.hidden_destinations = {int(o) for o in entry.data.get("hidden_destination_orders", [])}
        if client.hidden_sources or client.hidden_destinations:
            _LOGGER.debug(
                "[%s] Hidden profile rows: %d source(s), %d destination(s)",
                rname, len(client.hidden_sources), len(client.hidden_destinations),
            )

    hass.data[DOMAIN][entry.entry_id] = {
        "client": client,
        "route_listeners": route_listeners,
        "mnemonic_listeners": mnemonic_listeners,
        "mismatch_listeners": mismatch_listeners,
        "connection_listeners": connection_listeners,
        "lock_listeners": lock_listeners,
        "mismatch_orders": mismatch_orders,
    }

    await client.start()

    for _ in range(20):
        if client.connected:
            break
        await asyncio.sleep(0.5)
    else:
        await client.stop()
        raise ConfigEntryNotReady(
            f"Could not connect to Evertz Quartz router at "
            f"{entry.data[CONF_HOST]}:{entry.data[CONF_PORT]}"
        )

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    entry.async_on_unload(_cancel_finish_task)

    if not hass.services.has_service(DOMAIN, "route"):
        _register_route_service(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _register_route_service(hass: HomeAssistant) -> None:
    """Register evertz_quartz.route — targets a specific router when multiple exist."""

    async def _handle_route(call: ServiceCall) -> None:
        target_device_id = call.data.get(ATTR_DEVICE_ID)
        target_name      = call.data.get(ATTR_ROUTER_NAME)
        destination      = call.data[ATTR_DESTINATION]
        source           = call.data[ATTR_SOURCE]
        levels_override  = call.data.get(ATTR_LEVELS)

        entries = hass.config_entries.async_entries(DOMAIN)
        client: QuartzClient | None = None
        target_entry: ConfigEntry | None = None

        if target_device_id:
            dev_reg = dr.async_get(hass)
            device = dev_reg.async_get(target_device_id)
            if device is None:
                raise ServiceValidationError(f"Device '{target_device_id}' not found")
            for ident_domain, entry_id in device.identifiers:
                if ident_domain == DOMAIN:
                    client = hass.data.get(DOMAIN, {}).get(entry_id, {}).get("client")
                    target_entry = hass.config_entries.async_get_entry(entry_id)
                    break
            if client is None:
                raise ServiceValidationError(f"Device '{target_device_id}' has no active client")

        elif target_name:
            for entry in entries:
                if router_display_name(entry).lower() == target_name.lower():
                    client = hass.data.get(DOMAIN, {}).get(entry.entry_id, {}).get("client")
                    target_entry = entry
                    break
            if client is None:
                names = [router_display_name(e) for e in entries]
                raise ServiceValidationError(
                    f"Router '{target_name}' not found. Configured: {names}"
                )

        else:
            if len(entries) == 1:
                client = hass.data.get(DOMAIN, {}).get(entries[0].entry_id, {}).get("client")
                target_entry = entries[0]
            elif len(entries) == 0:
                raise ServiceValidationError("No Evertz Quartz routers configured")
            else:
                names = [router_display_name(e) for e in entries]
                raise ServiceValidationError(
                    f"Multiple routers configured ({names}). Specify device_id or router_name."
                )

        # Blocked takes raise ServiceValidationError for the caller AND go
        # through notify_blocked_route(), so automations/scripts that would
        # otherwise fail silently still surface an event + notification.

        # Read-only check — block takes from users not in the allowed list.
        # Calls without a user context (automations, scripts) are blocked too.
        if target_entry and not user_can_route(target_entry, destination, call.context.user_id):
            dest_name = client.destination_names.get(destination, f"Dest {destination}")
            notify_blocked_route(
                hass, target_entry, client,
                reason="read_only", dest_order=destination, src_order=source,
                user_id=call.context.user_id, origin="service",
            )
            raise ServiceValidationError(
                f"Route blocked: destination {dest_name!r} (Order {destination}) is "
                "read-only for this user. An administrator can change this in the "
                "integration's Configure panel."
            )

        # Lock check — block routing to locked destinations
        if client.locks.get(destination, 0) > 0:
            dest_name = client.destination_names.get(destination, f"Dest {destination}")
            if target_entry:
                notify_blocked_route(
                    hass, target_entry, client,
                    reason="locked", dest_order=destination, src_order=source,
                    user_id=call.context.user_id, origin="service",
                )
            raise ServiceValidationError(
                f"Route blocked: destination {dest_name!r} (Order {destination}) is locked. "
                "Unlock it before routing."
            )

        # Namespace check — block cross-namespace routes from service calls too
        dest_ns = client.destination_namespaces.get(destination)
        src_ns  = client.source_namespaces.get(source)
        if dest_ns and src_ns and dest_ns != src_ns:
            dest_name = client.destination_names.get(destination, f"Dest {destination}")
            src_name  = client.source_names.get(source, f"Source {source}")
            if target_entry:
                notify_blocked_route(
                    hass, target_entry, client,
                    reason="cross_namespace", dest_order=destination, src_order=source,
                    user_id=call.context.user_id, origin="service",
                )
            raise ServiceValidationError(
                f"Cross-namespace route blocked: source {src_name!r} (namespace={src_ns}) "
                f"cannot route to {dest_name!r} (namespace={dest_ns}). "
                f"Only {dest_ns} sources are valid for {dest_name!r}."
            )
        await client.route(destination, source, levels_override)

    hass.services.async_register(
        DOMAIN,
        "route",
        _handle_route,
        schema=vol.Schema({
            vol.Optional(ATTR_DEVICE_ID):   cv.string,
            vol.Optional(ATTR_ROUTER_NAME): cv.string,
            vol.Required(ATTR_DESTINATION): vol.All(int, vol.Range(min=1)),
            vol.Required(ATTR_SOURCE):      vol.All(int, vol.Range(min=1)),
            vol.Optional(ATTR_LEVELS):      cv.string,
        }),
    )


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    client: QuartzClient | None = data.get("client")
    if client:
        client.update_options(
            reconnect_delay=effective(entry, CONF_RECONNECT_DELAY, DEFAULT_RECONNECT_DELAY),
            connect_timeout=effective(entry, CONF_CONNECT_TIMEOUT, DEFAULT_CONNECT_TIMEOUT),
        )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        data = hass.data[DOMAIN].pop(entry.entry_id, {})
        client: QuartzClient = data.get("client")
        if client:
            await client.stop()
        # Don't leave a stale "synchronizing" message behind on unload/reload
        await hass.services.async_call(
            "persistent_notification", "dismiss",
            {"notification_id": f"evertz_quartz_{entry.entry_id}_startup_sync"},
        )
    return unload_ok
