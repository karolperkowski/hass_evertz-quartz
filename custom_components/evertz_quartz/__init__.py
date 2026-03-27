"""Evertz Quartz Router integration for Home Assistant."""

from __future__ import annotations

import asyncio
import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_CONNECT_TIMEOUT,
    CONF_LEVELS,
    CONF_MAX_DESTINATIONS,
    CONF_MAX_SOURCES,
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

PLATFORMS: list[Platform] = [Platform.SELECT]

ATTR_DESTINATION = "destination"
ATTR_SOURCE = "source"
ATTR_LEVELS = "levels"


def _effective(entry: ConfigEntry, key: str, default):
    """Return options value if set, otherwise fall back to data, then default.

    This lets the options flow override any value that was set during
    initial config — without touching entry.data.
    """
    if key in entry.options:
        return entry.options[key]
    return entry.data.get(key, default)


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
        _LOGGER.info(
            "Evertz Quartz router %s",
            "connected" if connected else "disconnected",
        )

    # All three editable values read from options first, then data
    max_sources      = _effective(entry, CONF_MAX_SOURCES,      DEFAULT_MAX_SOURCES)
    max_destinations = _effective(entry, CONF_MAX_DESTINATIONS,  DEFAULT_MAX_DESTINATIONS)
    levels           = _effective(entry, CONF_LEVELS,            DEFAULT_LEVELS)

    client = QuartzClient(
        host=entry.data[CONF_HOST],
        port=entry.data[CONF_PORT],
        max_sources=max_sources,
        max_destinations=max_destinations,
        levels=levels,
        route_callback=_route_callback,
        mnemonic_callback=_mnemonic_callback,
        connection_callback=_connection_callback,
        verbose_logging=_effective(entry, CONF_VERBOSE_LOGGING,  DEFAULT_VERBOSE_LOGGING),
        reconnect_delay=_effective(entry, CONF_RECONNECT_DELAY,  DEFAULT_RECONNECT_DELAY),
        connect_timeout=_effective(entry, CONF_CONNECT_TIMEOUT,  DEFAULT_CONNECT_TIMEOUT),
    )

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

    if not hass.services.has_service(DOMAIN, "route"):
        async def _handle_route_service(call: ServiceCall) -> None:
            destination = call.data[ATTR_DESTINATION]
            source = call.data[ATTR_SOURCE]
            levels_override = call.data.get(ATTR_LEVELS)
            await client.route(destination, source, levels_override)

        hass.services.async_register(
            DOMAIN,
            "route",
            _handle_route_service,
            schema=vol.Schema(
                {
                    vol.Required(ATTR_DESTINATION): vol.All(int, vol.Range(min=1)),
                    vol.Required(ATTR_SOURCE):      vol.All(int, vol.Range(min=1)),
                    vol.Optional(ATTR_LEVELS):      cv.string,
                }
            ),
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Called by HA when options are saved — options_flow handles live updates,
    this is a safety net that applies any debug/connection options that may
    have been missed (e.g. when the integration reloads)."""
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
