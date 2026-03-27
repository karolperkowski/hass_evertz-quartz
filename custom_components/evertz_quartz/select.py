"""Select platform for Evertz Quartz — one entity per router destination."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_LEVELS,
    CONF_MAX_DESTINATIONS,
    CONF_MAX_SOURCES,
    DEFAULT_LEVELS,
    DEFAULT_MAX_DESTINATIONS,
    DEFAULT_MAX_SOURCES,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


def _effective(entry: ConfigEntry, key: str, default):
    """Options override data, data overrides default."""
    if key in entry.options:
        return entry.options[key]
    return entry.data.get(key, default)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up one select entity per destination."""
    client = hass.data[DOMAIN][entry.entry_id]["client"]
    max_destinations = _effective(entry, CONF_MAX_DESTINATIONS, DEFAULT_MAX_DESTINATIONS)
    max_sources      = _effective(entry, CONF_MAX_SOURCES,      DEFAULT_MAX_SOURCES)

    entities = [
        QuartzDestinationSelect(entry, client, dest, max_sources)
        for dest in range(1, max_destinations + 1)
    ]
    async_add_entities(entities, update_before_add=True)


class QuartzDestinationSelect(SelectEntity):
    """One HA select entity = one router destination."""

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
    # Entity properties — all read live from client so they stay current
    # ------------------------------------------------------------------

    @property
    def name(self) -> str:
        """Destination mnemonic from router, or 'Destination N' fallback."""
        return (
            self._client.destination_names.get(self._destination)
            or f"Destination {self._destination}"
        )

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
        """Source list — reflects max_sources on the live client."""
        # Always read from client so a live max_sources update is reflected
        max_src = self._client.max_sources
        return [self._source_label(i) for i in range(1, max_src + 1)]

    @property
    def current_option(self) -> str | None:
        """Currently routed source for this destination."""
        src = self._client.routes.get(self._destination)
        if src is None:
            return None
        return self._source_label(src)

    @property
    def available(self) -> bool:
        return self._client._connected  # noqa: SLF001

    @property
    def extra_state_attributes(self) -> dict:
        """Expose destination number and current source number for automations."""
        src = self._client.routes.get(self._destination)
        return {
            "destination_number": self._destination,
            "source_number": src,
            "levels": self._client.levels,
        }

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    async def async_select_option(self, option: str) -> None:
        """Route the selected source to this destination."""
        src = self._label_to_source_number(option)
        if src is None:
            _LOGGER.warning(
                "Cannot route dest %d: unknown source label %r",
                self._destination, option,
            )
            return
        levels = self._client.levels
        await self._client.route(self._destination, src, levels)

    # ------------------------------------------------------------------
    # Push update listeners
    # ------------------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        """Register for route and mnemonic push updates."""
        entry_data = self.hass.data[DOMAIN][self._entry.entry_id]
        entry_data["route_listeners"].append(self._on_route_update)
        entry_data["mnemonic_listeners"].append(self._on_mnemonic_update)

    @callback
    def _on_route_update(self, dest: int, src: int, levels: str) -> None:
        if dest == self._destination:
            self.async_write_ha_state()

    @callback
    def _on_mnemonic_update(self) -> None:
        """Redraw on any name or size change — options list and entity name."""
        self.async_write_ha_state()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _source_label(self, src_num: int) -> str:
        return self._client.source_names.get(src_num) or f"Source {src_num}"

    def _label_to_source_number(self, label: str) -> int | None:
        for num in range(1, self._client.max_sources + 1):
            if self._source_label(num) == label:
                return num
        return None
