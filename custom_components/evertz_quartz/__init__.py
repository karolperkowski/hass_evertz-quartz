"""Evertz Quartz Router integration for Home Assistant."""

from __future__ import annotations

import asyncio
import logging

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
from .helpers import effective, router_display_name
from .quartz_client import QuartzClient

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SELECT, Platform.BUTTON, Platform.BINARY_SENSOR]

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
    mismatch_orders: set = set()   # (kind, order) pairs seen out-of-range this session

    def _route_callback(dest: int, src: int, levels: str) -> None:
        for cb in route_listeners:
            hass.loop.call_soon_threadsafe(cb, dest, src, levels)

    def _mnemonic_callback() -> None:
        for cb in mnemonic_listeners:
            hass.loop.call_soon_threadsafe(cb)

    def _connection_callback(connected: bool) -> None:
        name = router_display_name(entry)
        _LOGGER.info("Evertz Quartz [%s] %s", name, "connected" if connected else "disconnected")

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
                        f"The router reported {label} Order **{order}** which is outside "
                        f"the configured range (current maximum: {limit}).\n\n"
                        "Your router profile may have expanded or changed.\n\n"
                        "**To fix:** Go to Settings \u2192 Devices & Services \u2192 "
                        "Evertz Quartz \u2192 Configure and select **Update Profile** "
                        "to re-import your CSV or adjust the counts manually."
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

    hass.data[DOMAIN][entry.entry_id] = {
        "client": client,
        "route_listeners": route_listeners,
        "mnemonic_listeners": mnemonic_listeners,
        "mismatch_listeners": mismatch_listeners,
        "mismatch_orders": mismatch_orders,
    }

    await client.start()

    for _ in range(20):
        if client._connected:  # noqa: SLF001
            break
        await asyncio.sleep(0.5)
    else:
        await client.stop()
        raise ConfigEntryNotReady(
            f"Could not connect to Evertz Quartz router at "
            f"{entry.data[CONF_HOST]}:{entry.data[CONF_PORT]}"
        )

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

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

        if target_device_id:
            dev_reg = dr.async_get(hass)
            device = dev_reg.async_get(target_device_id)
            if device is None:
                raise ServiceValidationError(f"Device '{target_device_id}' not found")
            for ident_domain, entry_id in device.identifiers:
                if ident_domain == DOMAIN:
                    client = hass.data.get(DOMAIN, {}).get(entry_id, {}).get("client")
                    break
            if client is None:
                raise ServiceValidationError(f"Device '{target_device_id}' has no active client")

        elif target_name:
            for entry in entries:
                if router_display_name(entry).lower() == target_name.lower():
                    client = hass.data.get(DOMAIN, {}).get(entry.entry_id, {}).get("client")
                    break
            if client is None:
                names = [router_display_name(e) for e in entries]
                raise ServiceValidationError(
                    f"Router '{target_name}' not found. Configured: {names}"
                )

        else:
            if len(entries) == 1:
                client = hass.data.get(DOMAIN, {}).get(entries[0].entry_id, {}).get("client")
            elif len(entries) == 0:
                raise ServiceValidationError("No Evertz Quartz routers configured")
            else:
                names = [router_display_name(e) for e in entries]
                raise ServiceValidationError(
                    f"Multiple routers configured ({names}). Specify device_id or router_name."
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
    return unload_ok
