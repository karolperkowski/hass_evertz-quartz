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
    CONF_LEVELS,
    CONF_MAX_DESTINATIONS,
    CONF_MAX_SOURCES,
    CONF_NAME,
    CONF_RECONNECT_DELAY,
    CONF_VERBOSE_LOGGING,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_LEVELS,
    DEFAULT_MAX_DESTINATIONS,
    DEFAULT_MAX_SOURCES,
    DEFAULT_RECONNECT_DELAY,
    DEFAULT_VERBOSE_LOGGING,
    DOMAIN,
)
from .quartz_client import QuartzClient

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SELECT, Platform.BUTTON]

ATTR_DESTINATION  = "destination"
ATTR_SOURCE       = "source"
ATTR_LEVELS       = "levels"
ATTR_DEVICE_ID    = "device_id"
ATTR_ROUTER_NAME  = "router_name"


def _effective(entry: ConfigEntry, key: str, default):
    """Options override data, data overrides default."""
    if key in entry.options:
        return entry.options[key]
    return entry.data.get(key, default)


def _router_display_name(entry: ConfigEntry) -> str:
    """Human-readable router name: CONF_NAME → host IP."""
    return entry.data.get(CONF_NAME) or entry.data.get(CONF_HOST, "Unknown Router")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Evertz Quartz from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    route_listeners: list = []
    mnemonic_listeners: list = []

    def _route_callback(dest: int, src: int, levels: str) -> None:
        for cb in route_listeners:
            hass.loop.call_soon_threadsafe(cb, dest, src, levels)

    def _mnemonic_callback() -> None:
        for cb in mnemonic_listeners:
            hass.loop.call_soon_threadsafe(cb)

    def _connection_callback(connected: bool) -> None:
        name = _router_display_name(entry)
        _LOGGER.info("Evertz Quartz [%s] %s", name, "connected" if connected else "disconnected")

    client = QuartzClient(
        host=entry.data[CONF_HOST],
        port=entry.data[CONF_PORT],
        max_sources=_effective(entry, CONF_MAX_SOURCES,     DEFAULT_MAX_SOURCES),
        max_destinations=_effective(entry, CONF_MAX_DESTINATIONS, DEFAULT_MAX_DESTINATIONS),
        levels=_effective(entry, CONF_LEVELS,           DEFAULT_LEVELS),
        route_callback=_route_callback,
        mnemonic_callback=_mnemonic_callback,
        connection_callback=_connection_callback,
        verbose_logging=_effective(entry, CONF_VERBOSE_LOGGING,  DEFAULT_VERBOSE_LOGGING),
        reconnect_delay=_effective(entry, CONF_RECONNECT_DELAY,  DEFAULT_RECONNECT_DELAY),
        connect_timeout=_effective(entry, CONF_CONNECT_TIMEOUT,  DEFAULT_CONNECT_TIMEOUT),
    )

    # Pre-populate names from CSV stored in entry.data (survives reloads)
    if stored_src := entry.data.get("source_names"):
        client.source_names.update({int(k): v for k, v in stored_src.items()})
        _LOGGER.debug("Loaded %d source names from stored profile", len(stored_src))
    if stored_dst := entry.data.get("destination_names"):
        client.destination_names.update({int(k): v for k, v in stored_dst.items()})
        _LOGGER.debug("Loaded %d destination names from stored profile", len(stored_dst))

    hass.data[DOMAIN][entry.entry_id] = {
        "client": client,
        "route_listeners": route_listeners,
        "mnemonic_listeners": mnemonic_listeners,
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

    # ── Register the multi-router route service (once) ────────────────────
    if not hass.services.has_service(DOMAIN, "route"):
        _register_route_service(hass)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _register_route_service(hass: HomeAssistant) -> None:
    """
    Register evertz_quartz.route once.

    Targeting (at least one required when multiple routers configured):
      device_id   — HA device registry ID of the router device
      router_name — friendly name / IP matching CONF_NAME or CONF_HOST
    If neither is supplied and exactly one router is configured, it is
    used automatically.
    """

    async def _handle_route(call: ServiceCall) -> None:
        target_device_id = call.data.get(ATTR_DEVICE_ID)
        target_name      = call.data.get(ATTR_ROUTER_NAME)
        destination      = call.data[ATTR_DESTINATION]
        source           = call.data[ATTR_SOURCE]
        levels_override  = call.data.get(ATTR_LEVELS)

        entries = hass.config_entries.async_entries(DOMAIN)
        client: QuartzClient | None = None

        if target_device_id:
            # Look up device, find matching entry via identifiers
            dev_reg = dr.async_get(hass)
            device = dev_reg.async_get(target_device_id)
            if device is None:
                raise ServiceValidationError(f"Device '{target_device_id}' not found")
            # identifiers = {(DOMAIN, entry_id)}
            for ident_domain, entry_id in device.identifiers:
                if ident_domain == DOMAIN:
                    data = hass.data.get(DOMAIN, {}).get(entry_id, {})
                    client = data.get("client")
                    break
            if client is None:
                raise ServiceValidationError(
                    f"Device '{target_device_id}' found but no active Quartz client"
                )

        elif target_name:
            # Match against CONF_NAME or CONF_HOST
            for entry in entries:
                if _router_display_name(entry).lower() == target_name.lower():
                    data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
                    client = data.get("client")
                    break
            if client is None:
                names = [_router_display_name(e) for e in entries]
                raise ServiceValidationError(
                    f"Router '{target_name}' not found. Configured routers: {names}"
                )

        else:
            # No target — auto-select only if exactly one router configured
            if len(entries) == 1:
                data = hass.data.get(DOMAIN, {}).get(entries[0].entry_id, {})
                client = data.get("client")
            elif len(entries) == 0:
                raise ServiceValidationError("No Evertz Quartz routers configured")
            else:
                names = [_router_display_name(e) for e in entries]
                raise ServiceValidationError(
                    f"Multiple routers configured ({names}). "
                    f"Specify device_id or router_name."
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
    """Safety-net: re-apply connection/debug options after any options save."""
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    client: QuartzClient | None = data.get("client")
    if client:
        client.update_options(
            verbose_logging=_effective(entry, CONF_VERBOSE_LOGGING, DEFAULT_VERBOSE_LOGGING),
            reconnect_delay=_effective(entry, CONF_RECONNECT_DELAY, DEFAULT_RECONNECT_DELAY),
            connect_timeout=_effective(entry, CONF_CONNECT_TIMEOUT, DEFAULT_CONNECT_TIMEOUT),
        )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        data = hass.data[DOMAIN].pop(entry.entry_id, {})
        client: QuartzClient = data.get("client")
        if client:
            await client.stop()
    return unload_ok
