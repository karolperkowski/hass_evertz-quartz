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
    DEFAULT_MAX_DESTINATIONS,
    DOMAIN,
)
from .helpers import effective, router_display_name

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


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=router_display_name(entry),
        manufacturer="Evertz",
        model="EQX / EQT Router",
        configuration_url=f"http://{entry.data.get('host', '')}",
    )


def _current_log_level(logger_name: str = "custom_components.evertz_quartz") -> str:
    logger = logging.getLogger(logger_name)
    name = logging.getLevelName(logger.level)
    return name if name in _LOG_LEVELS else _DEFAULT_LOG_LEVEL


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up destination selects + log level controls."""
    client = hass.data[DOMAIN][entry.entry_id]["client"]
    max_destinations = effective(entry, CONF_MAX_DESTINATIONS, DEFAULT_MAX_DESTINATIONS)

    entities: list[SelectEntity] = [
        QuartzDestinationSelect(entry=entry, client=client, order=dest)
        for dest in range(1, max_destinations + 1)
    ]
    entities.append(QuartzLogLevelSelect(entry, "integration"))
    entities.append(QuartzLogLevelSelect(entry, "client"))
    async_add_entities(entities, update_before_add=True)


class QuartzDestinationSelect(SelectEntity):
    """
    One HA select entity per router destination.
    All numbers are Order indices — MAGNUM uses Order in all protocol messages.
    """

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(self, entry: ConfigEntry, client, order: int) -> None:
        self._entry = entry
        self._client = client
        self._order = order   # destination Order index (MAGNUM numbering)
        self._attr_unique_id = f"{entry.entry_id}_dest_{order}"

    @property
    def name(self) -> str:
        return (
            self._client.destination_names.get(self._order)
            or f"Destination {self._order}"
        )

    @property
    def device_info(self) -> DeviceInfo:
        return _device_info(self._entry)

    @property
    def options(self) -> list[str]:
        return [
            self._client.source_names.get(i) or f"Source {i}"
            for i in range(1, self._client.max_sources + 1)
        ]

    @property
    def current_option(self) -> str | None:
        src_order = self._client.routes.get(self._order)
        if src_order is None:
            return None
        return self._client.source_names.get(src_order) or f"Source {src_order}"

    @property
    def available(self) -> bool:
        return self._client._connected  # noqa: SLF001

    @property
    def extra_state_attributes(self) -> dict:
        src_order = self._client.routes.get(self._order)
        return {
            "destination_order": self._order,
            "source_order":      src_order,
            "levels":            self._client.levels,
            "router":            self._entry.data.get("router_name") or self._entry.data.get("host", ""),
        }

    async def async_select_option(self, option: str) -> None:
        """Find source Order by label and route."""
        src_order = next(
            (i for i in range(1, self._client.max_sources + 1)
             if (self._client.source_names.get(i) or f"Source {i}") == option),
            None,
        )
        if src_order is None:
            _LOGGER.warning("Cannot route dest %d: unknown source %r", self._order, option)
            return
        await self._client.route(self._order, src_order, self._client.levels)

    async def async_added_to_hass(self) -> None:
        entry_data = self.hass.data[DOMAIN][self._entry.entry_id]
        entry_data["route_listeners"].append(self._on_route_update)
        entry_data["mnemonic_listeners"].append(self._on_mnemonic_update)

    @callback
    def _on_route_update(self, dest_order: int, src_order: int, levels: str) -> None:
        if dest_order == self._order:
            self.async_write_ha_state()

    @callback
    def _on_mnemonic_update(self) -> None:
        self.async_write_ha_state()


# Loggers controlled by each entity
_INTEGRATION_ONLY_LOGGERS = [
    "custom_components.evertz_quartz",
    "custom_components.evertz_quartz.select",
    "custom_components.evertz_quartz.button",
    "custom_components.evertz_quartz.config_flow",
    "custom_components.evertz_quartz.options_flow",
]
_CLIENT_LOGGER = "custom_components.evertz_quartz.quartz_client"


class QuartzLogLevelSelect(SelectEntity):
    """
    Log level control — two instances:
      mode="integration"  controls all loggers except quartz_client
      mode="client"       controls quartz_client only (TCP protocol detail)
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:math-log"
    _attr_options = _LOG_LEVELS
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, mode: str) -> None:
        self._entry = entry
        self._mode = mode   # "integration" or "client"
        self._attr_unique_id = f"{entry.entry_id}_log_level_{mode}"
        # Restore persisted level from options, fall back to current logger level
        opt_key = "client_log_level" if mode == "client" else "integration_log_level"
        persisted = entry.options.get(opt_key)
        if persisted and persisted in _LOG_LEVELS:
            # Apply persisted level immediately on startup
            numeric = getattr(logging, persisted)
            if mode == "client":
                logging.getLogger(_CLIENT_LOGGER).setLevel(numeric)
            else:
                for name in _INTEGRATION_ONLY_LOGGERS:
                    logging.getLogger(name).setLevel(numeric)
            self._current = persisted
        else:
            self._current = _current_log_level(
                _CLIENT_LOGGER if mode == "client" else "custom_components.evertz_quartz"
            )

    @property
    def name(self) -> str:
        return "Client Log Level" if self._mode == "client" else "Log Level"

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
        if self._mode == "client":
            logging.getLogger(_CLIENT_LOGGER).setLevel(numeric)
            _LOGGER.info("Client (quartz_client) log level set to %s", option)
        else:
            for name in _INTEGRATION_ONLY_LOGGERS:
                logging.getLogger(name).setLevel(numeric)
            _LOGGER.info("Integration log level set to %s", option)
        self._current = option
        self.async_write_ha_state()
        # Persist to options so level survives HA restarts
        opt_key = "client_log_level" if self._mode == "client" else "integration_log_level"
        new_options = dict(self._entry.options)
        new_options[opt_key] = option
        self.hass.config_entries.async_update_entry(self._entry, options=new_options)
