"""Select platform for Evertz Quartz — one entity per router destination."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_MAX_DESTINATIONS,
    CONF_MAX_SOURCES,
    CONF_LEVELS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Evertz Quartz select entities from a config entry."""
    client = hass.data[DOMAIN][entry.entry_id]["client"]
    max_destinations = entry.data[CONF_MAX_DESTINATIONS]
    max_sources = entry.data[CONF_MAX_SOURCES]

    entities = [
        QuartzDestinationSelect(entry, client, dest, max_sources)
        for dest in range(1, max_destinations + 1)
    ]
    async_add_entities(entities, update_before_add=True)


class QuartzDestinationSelect(SelectEntity):
    """Represents a single router destination as a HA select entity."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(
        self,
        entry: ConfigEntry,
        client,
        destination: int,
        max_sources: int,
    ) -> None:
        self._entry = entry
        self._client = client
        self._destination = destination
        self._max_sources = max_sources

        self._attr_unique_id = f"{entry.entry_id}_dest_{destination}"

    # ------------------------------------------------------------------
    # Entity properties
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        """Human-readable destination name (from mnemonic or fallback)."""
        mnemonic = self._client.destination_names.get(self._destination)
        return mnemonic or f"Destination {self._destination}"

    @property
    def device_info(self) -> DeviceInfo:
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name="Evertz Quartz Router",
            manufacturer="Evertz",
            model="EQX / EQT Router",
        )

    @property
    def options(self) -> list[str]:
        """Return the list of available sources."""
        return [self._source_label(i) for i in range(1, self._max_sources + 1)]

    @property
    def current_option(self) -> str | None:
        """Return the currently routed source."""
        src = self._client.routes.get(self._destination)
        if src is None:
            return None
        return self._source_label(src)

    @property
    def available(self) -> bool:
        return self._client._connected  # noqa: SLF001

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    async def async_select_option(self, option: str) -> None:
        """Route the selected source to this destination."""
        src = self._label_to_source_number(option)
        if src is None:
            _LOGGER.warning("Unknown source option: %s", option)
            return
        levels = self._entry.data.get(CONF_LEVELS, "V")
        await self._client.route(self._destination, src, levels)

    # ------------------------------------------------------------------
    # Real-time push updates
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        """Register as a listener for route updates."""
        self.hass.data[DOMAIN][self._entry.entry_id]["listeners"].append(
            self._on_route_update
        )

    @callback
    def _on_route_update(self, dest: int, src: int, levels: str) -> None:
        """Called by the client whenever the router reports a route change."""
        if dest == self._destination:
            self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _source_label(self, src_num: int) -> str:
        """Return mnemonic name if available, else 'Source N'."""
        return self._client.source_names.get(src_num) or f"Source {src_num}"

    def _label_to_source_number(self, label: str) -> int | None:
        """Reverse-map a label back to a source number."""
        for num in range(1, self._max_sources + 1):
            if self._source_label(num) == label:
                return num
        return None
