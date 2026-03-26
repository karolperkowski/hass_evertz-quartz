"""Evertz Quartz Router integration for Home Assistant."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady

from .const import (
    CONF_LEVELS,
    CONF_MAX_DESTINATIONS,
    CONF_MAX_SOURCES,
    DOMAIN,
)
from .quartz_client import QuartzClient

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SELECT]

# Service schema fields
ATTR_DESTINATION = "destination"
ATTR_SOURCE = "source"
ATTR_LEVELS = "levels"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Evertz Quartz from a config entry."""

    hass.data.setdefault(DOMAIN, {})

    listeners: list = []

    def _route_callback(dest: int, src: int, levels: str) -> None:
        """Dispatch route updates to all registered entity listeners."""
        for cb in listeners:
            hass.loop.call_soon_threadsafe(cb, dest, src, levels)

    def _connection_callback(connected: bool) -> None:
        _LOGGER.info(
            "Evertz Quartz router %s",
            "connected" if connected else "disconnected",
        )

    client = QuartzClient(
        host=entry.data[CONF_HOST],
        port=entry.data[CONF_PORT],
        max_sources=entry.data[CONF_MAX_SOURCES],
        max_destinations=entry.data[CONF_MAX_DESTINATIONS],
        levels=entry.data.get(CONF_LEVELS, "V"),
        route_callback=_route_callback,
        connection_callback=_connection_callback,
    )

    hass.data[DOMAIN][entry.entry_id] = {
        "client": client,
        "listeners": listeners,
    }

    # Start the background TCP loop
    await client.start()

    # Give it a moment to connect; raise ConfigEntryNotReady if it fails
    import asyncio
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

    # Register the `evertz_quartz.route` service
    async def _handle_route_service(call: ServiceCall) -> None:
        destination = call.data[ATTR_DESTINATION]
        source = call.data[ATTR_SOURCE]
        levels = call.data.get(ATTR_LEVELS)
        await client.route(destination, source, levels)

    if not hass.services.has_service(DOMAIN, "route"):
        import voluptuous as vol
        from homeassistant.helpers import config_validation as cv

        hass.services.async_register(
            DOMAIN,
            "route",
            _handle_route_service,
            schema=vol.Schema(
                {
                    vol.Required(ATTR_DESTINATION): vol.All(int, vol.Range(min=1)),
                    vol.Required(ATTR_SOURCE): vol.All(int, vol.Range(min=1)),
                    vol.Optional(ATTR_LEVELS): cv.string,
                }
            ),
        )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        data = hass.data[DOMAIN].pop(entry.entry_id, {})
        client: QuartzClient = data.get("client")
        if client:
            await client.stop()

    return unload_ok
