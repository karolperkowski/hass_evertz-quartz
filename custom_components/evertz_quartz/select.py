"""Select platform for Evertz Quartz — destination routing + log level."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_MAX_DESTINATIONS,
    CONF_MAX_SOURCES,
    DEFAULT_MAX_DESTINATIONS,
    DEFAULT_MAX_SOURCES,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

_INTEGRATION_LOGGERS = [
    "custom_components.evertz_quartz",
    "custom_components.evertz_quartz.quartz_client",
    "custom_components.evertz_quartz.select",
    "custom_components.evertz_quartz.button",
    "custom_components.evertz_quartz.config_flow",
    "custom_components.evertz_quartz.options_flow",
]
_LOG_LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR"]
_DEFAULT_LOG_LEVEL = "WARNING"


def _effective(entry: ConfigEntry, key: str, default):
    if key in entry.options:
        return entry.options[key]
    return entry.data.get(key, default)


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    from . import _router_display_name
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=_router_display_name(entry),
        manufacturer="Evertz",
        model="EQX / EQT Router",
        configuration_url=f"http://{entry.data.get('host', '')}",
    )


def _current_log_level() -> str:
    logger = logging.getLogger("custom_components.evertz_quartz")
    name = logging.getLevelName(logger.level)
    return name if name in _LOG_LEVELS else _DEFAULT_LOG_LEVEL


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up destination selects + log level control."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    client = entry_data["client"]
    src_port_map = entry_data.get("src_port_map", {})   # {order: quartz_port}
    dst_port_map = entry_data.get("dst_port_map", {})   # {order: quartz_port}

    max_destinations = _effective(entry, CONF_MAX_DESTINATIONS, DEFAULT_MAX_DESTINATIONS)
    max_sources      = _effective(entry, CONF_MAX_SOURCES,      DEFAULT_MAX_SOURCES)

    entities: list[SelectEntity] = [
        QuartzDestinationSelect(
            entry=entry,
            client=client,
            order=dest,
            quartz_port=dst_port_map.get(dest, dest),   # identity fallback when no map
            max_sources=max_sources,
            src_port_map=src_port_map,
        )
        for dest in range(1, max_destinations + 1)
    ]
    entities.append(QuartzLogLevelSelect(entry))
    async_add_entities(entities, update_before_add=True)


class QuartzDestinationSelect(SelectEntity):
    """
    One HA select entity per router destination.

    self._order      = profile Order index (1-based) — entity unique_id, name fallback
    self._quartz_port = Quartz crosspoint address   — used in .SV commands + .UV matching
    These are the same number for contiguous routers, different for tieline/remote sources.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(
        self,
        entry: ConfigEntry,
        client,
        order: int,
        quartz_port: int,
        max_sources: int,
        src_port_map: dict[int, int],
    ) -> None:
        self._entry = entry
        self._client = client
        self._order = order
        self._quartz_port = quartz_port
        self._max_sources = max_sources
        self._src_port_map = src_port_map  # {src_order: src_quartz_port}
        self._attr_unique_id = f"{entry.entry_id}_dest_{order}"

    @property
    def name(self) -> str:
        """Destination name from router mnemonic (keyed by port) or fallback."""
        return (
            self._client.destination_names.get(self._quartz_port)
            or f"Destination {self._order}"
        )

    @property
    def device_info(self) -> DeviceInfo:
        return _device_info(self._entry)

    @property
    def options(self) -> list[str]:
        """Source options — uses mnemonic names when available."""
        return [self._source_label(order) for order in range(1, self._client.max_sources + 1)]

    @property
    def current_option(self) -> str | None:
        """Currently routed source, looked up by Quartz port then mapped to display label."""
        src_port = self._client.routes.get(self._quartz_port)
        if src_port is None:
            return None
        # Find which source order has this port, then display its label
        src_order = self._port_to_src_order(src_port)
        return self._source_label(src_order)

    @property
    def available(self) -> bool:
        return self._client._connected  # noqa: SLF001

    @property
    def extra_state_attributes(self) -> dict:
        src_port = self._client.routes.get(self._quartz_port)
        return {
            "destination_order":      self._order,
            "destination_quartz_port":self._quartz_port,
            "source_quartz_port":     src_port,
            "levels":                 self._client.levels,
            "router":                 self._entry.data.get("router_name") or self._entry.data.get("host", ""),
        }

    async def async_select_option(self, option: str) -> None:
        """Route: translate display label → source order → Quartz port → send command."""
        src_order = self._label_to_src_order(option)
        if src_order is None:
            _LOGGER.warning(
                "Cannot route dest order %d (port %d): unknown source %r",
                self._order, self._quartz_port, option,
            )
            return
        src_port = self._src_port_map.get(src_order, src_order)
        await self._client.route(self._quartz_port, src_port, self._client.levels)

    async def async_added_to_hass(self) -> None:
        entry_data = self.hass.data[DOMAIN][self._entry.entry_id]
        entry_data["route_listeners"].append(self._on_route_update)
        entry_data["mnemonic_listeners"].append(self._on_mnemonic_update)

    @callback
    def _on_route_update(self, dest_port: int, src_port: int, levels: str) -> None:
        """Router sent .UV — check if it's our destination's Quartz port."""
        if dest_port == self._quartz_port:
            self.async_write_ha_state()

    @callback
    def _on_mnemonic_update(self) -> None:
        self.async_write_ha_state()

    # ── Helpers ───────────────────────────────────────────────────────────

    def _source_label(self, src_order: int) -> str:
        """Display name for a source given its profile Order index."""
        src_port = self._src_port_map.get(src_order, src_order)
        return self._client.source_names.get(src_port) or f"Source {src_order}"

    def _label_to_src_order(self, label: str) -> int | None:
        """Reverse-map a display label back to a source Order index."""
        for order in range(1, self._client.max_sources + 1):
            if self._source_label(order) == label:
                return order
        return None

    def _port_to_src_order(self, port: int) -> int:
        """Map a Quartz source port number back to its profile Order index."""
        for order, p in self._src_port_map.items():
            if p == port:
                return order
        return port  # identity fallback when no map


class QuartzLogLevelSelect(SelectEntity):
    """Integration log level control — diagnostic entity."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:math-log"
    _attr_options = _LOG_LEVELS
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_log_level"
        self._current = _current_log_level()

    @property
    def name(self) -> str:
        return "Log Level"

    @property
    def current_option(self) -> str:
        return self._current

    @property
    def device_info(self) -> DeviceInfo:
        return _device_info(self._entry)

    async def async_select_option(self, option: str) -> None:
        if option not in _LOG_LEVELS:
            return
        numeric = getattr(logging, option)
        for name in _INTEGRATION_LOGGERS:
            logging.getLogger(name).setLevel(numeric)
        self._current = option
        self.async_write_ha_state()
        _LOGGER.info("Log level set to %s", option)
