"""Button platform for Evertz Quartz — resync controls."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    from . import _router_display_name
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=_router_display_name(entry),
        manufacturer="Evertz",
        model="EQX / EQT Router",
        configuration_url=f"http://{entry.data.get('host', '')}",
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    client = hass.data[DOMAIN][entry.entry_id]["client"]
    async_add_entities([
        QuartzResyncButton(entry, client, "full"),
        QuartzResyncButton(entry, client, "routes"),
        QuartzResyncButton(entry, client, "names"),
    ])


class QuartzResyncButton(ButtonEntity):
    """Triggers a re-poll of router state."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    _MODES = {
        "full":   ("Resync All",    "mdi:refresh"),
        "routes": ("Resync Routes", "mdi:routes"),
        "names":  ("Resync Names",  "mdi:rename-box"),
    }

    def __init__(self, entry: ConfigEntry, client, mode: str) -> None:
        self._entry = entry
        self._client = client
        self._mode = mode
        name, icon = self._MODES[mode]
        self._attr_unique_id = f"{entry.entry_id}_resync_{mode}"
        self._attr_icon = icon

    @property
    def name(self) -> str:
        return self._MODES[self._mode][0]

    @property
    def device_info(self) -> DeviceInfo:
        return _device_info(self._entry)

    @property
    def available(self) -> bool:
        return self._client._connected  # noqa: SLF001

    async def async_press(self) -> None:
        _LOGGER.info("[%s] Manual resync: %s", self._entry.data.get("router_name", self._entry.data.get("host", "")), self._mode)
        if self._mode in ("full", "names"):
            await self._client.query_all_mnemonics()
        if self._mode in ("full", "routes"):
            await self._client.query_all_routes()
