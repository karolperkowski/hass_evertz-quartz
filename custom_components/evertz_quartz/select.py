"""Select platform for Evertz Quartz — destination routing + log level."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
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

# ── Log level entity ─────────────────────────────────────────────────────────

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


def _current_log_level() -> str:
    """Read the effective level of the root integration logger."""
    logger = logging.getLogger("custom_components.evertz_quartz")
    name = logging.getLevelName(logger.level)
    return name if name in _LOG_LEVELS else _DEFAULT_LOG_LEVEL


def _effective(entry: ConfigEntry, key: str, default):
    """Options override data, data overrides default."""
    if key in entry.options:
        return entry.options[key]
    return entry.data.get(key, default)


# ── Platform setup ────────────────────────────────────────────────────────────

async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up destination select entities + log level control."""
    client = hass.data[DOMAIN][entry.entry_id]["client"]
    max_destinations = _effective(entry, CONF_MAX_DESTINATIONS, DEFAULT_MAX_DESTINATIONS)
    max_sources      = _effective(entry, CONF_MAX_SOURCES,      DEFAULT_MAX_SOURCES)

    entities: list[SelectEntity] = [
        QuartzDestinationSelect(entry, client, dest, max_sources)
        for dest in range(1, max_destinations + 1)
    ]
    entities.append(QuartzLogLevelSelect(entry))
    async_add_entities(entities, update_before_add=True)


# ── Destination routing entity ────────────────────────────────────────────────

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

    @property
    def name(self) -> str:
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
        return [self._source_label(i) for i in range(1, self._client.max_sources + 1)]

    @property
    def current_option(self) -> str | None:
        src = self._client.routes.get(self._destination)
        return self._source_label(src) if src is not None else None

    @property
    def available(self) -> bool:
        return self._client._connected  # noqa: SLF001

    @property
    def extra_state_attributes(self) -> dict:
        src = self._client.routes.get(self._destination)
        return {
            "destination_number": self._destination,
            "source_number": src,
            "levels": self._client.levels,
        }

    async def async_select_option(self, option: str) -> None:
        src = self._label_to_source_number(option)
        if src is None:
            _LOGGER.warning("Cannot route dest %d: unknown source %r", self._destination, option)
            return
        await self._client.route(self._destination, src, self._client.levels)

    async def async_added_to_hass(self) -> None:
        entry_data = self.hass.data[DOMAIN][self._entry.entry_id]
        entry_data["route_listeners"].append(self._on_route_update)
        entry_data["mnemonic_listeners"].append(self._on_mnemonic_update)

    @callback
    def _on_route_update(self, dest: int, src: int, levels: str) -> None:
        if dest == self._destination:
            self.async_write_ha_state()

    @callback
    def _on_mnemonic_update(self) -> None:
        self.async_write_ha_state()

    def _source_label(self, src_num: int) -> str:
        return self._client.source_names.get(src_num) or f"Source {src_num}"

    def _label_to_source_number(self, label: str) -> int | None:
        for num in range(1, self._client.max_sources + 1):
            if self._source_label(num) == label:
                return num
        return None


# ── Log level control entity ──────────────────────────────────────────────────

class QuartzLogLevelSelect(SelectEntity):
    """Select entity to control integration log level at runtime."""

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
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name="Evertz Quartz Router",
            manufacturer="Evertz",
            model="EQX / EQT Router",
        )

    async def async_select_option(self, option: str) -> None:
        """Apply log level to all integration loggers instantly."""
        if option not in _LOG_LEVELS:
            return
        numeric = getattr(logging, option)
        for name in _INTEGRATION_LOGGERS:
            logging.getLogger(name).setLevel(numeric)
        self._current = option
        self.async_write_ha_state()
        _LOGGER.info("Log level set to %s", option)
