"""Button platform for Evertz Quartz — resync + clear CSV profile."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_CSV_LOADED, DOMAIN

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
        QuartzClearCsvButton(entry, client),
    ])


class QuartzResyncButton(ButtonEntity):
    """Manually re-poll routes or names from the router."""

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
        self._attr_unique_id = f"{entry.entry_id}_resync_{mode}"
        self._attr_icon = self._MODES[mode][1]

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
        router = self._entry.data.get("router_name") or self._entry.data.get("host", "")
        _LOGGER.info("[%s] Manual resync: %s", router, self._mode)
        if self._mode in ("full", "names"):
            await self._client.query_all_mnemonics()
        if self._mode in ("full", "routes"):
            await self._client.query_all_routes()


class QuartzClearCsvButton(ButtonEntity):
    """
    Clear the loaded CSV profile.

    After pressing, entity names revert to 'Source N' / 'Destination N'
    (or whatever the router responds to .RT/.RD with on next resync).
    Port maps are also cleared, reverting to sequential 1:1 mapping.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:file-remove-outline"

    def __init__(self, entry: ConfigEntry, client) -> None:
        self._entry = entry
        self._client = client
        self._attr_unique_id = f"{entry.entry_id}_clear_csv"

    @property
    def name(self) -> str:
        return "Clear CSV Profile"

    @property
    def device_info(self) -> DeviceInfo:
        return _device_info(self._entry)

    async def async_press(self) -> None:
        router = self._entry.data.get("router_name") or self._entry.data.get("host", "")
        _LOGGER.info("[%s] Clearing CSV profile — reverting to router names", router)

        # Clear CSV flag and stored data
        new_data = dict(self._entry.data)
        new_data[CONF_CSV_LOADED] = False
        new_data.pop("source_names", None)
        new_data.pop("destination_names", None)
        new_data.pop("source_port_map", None)
        new_data.pop("destination_port_map", None)
        self.hass.config_entries.async_update_entry(self._entry, data=new_data)

        # Clear client state
        self._client.csv_loaded = False
        self._client.source_names.clear()
        self._client.destination_names.clear()
        # Restore identity port maps
        max_src = self._client.max_sources
        max_dst = self._client.max_destinations
        self._client.src_port_map = {n: n for n in range(1, max_src + 1)}
        self._client.dst_port_map = {n: n for n in range(1, max_dst + 1)}

        # Update hass.data port maps too
        self.hass.data[DOMAIN][self._entry.entry_id]["src_port_map"] = self._client.src_port_map
        self.hass.data[DOMAIN][self._entry.entry_id]["dst_port_map"] = self._client.dst_port_map

        # Fire mnemonic callback so all entities redraw with fallback names
        for cb in self.hass.data[DOMAIN][self._entry.entry_id].get("mnemonic_listeners", []):
            self.hass.loop.call_soon_threadsafe(cb)

        # Try querying router for names now that csv_loaded is False
        if self._client._connected:  # noqa: SLF001
            await self._client.query_all_mnemonics()
