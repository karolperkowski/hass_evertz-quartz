"""Button platform for Evertz Quartz — resync + clear CSV profile."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_CSV_LOADED, DOMAIN
from .helpers import apply_resize, router_display_name

_LOGGER = logging.getLogger(__name__)


def _device_info(entry: ConfigEntry):
    from .helpers import device_info as _di
    return _di(entry)


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
        QuartzResizeButton(entry, client),
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


class QuartzResizeButton(ButtonEntity):
    """One-click resize: grow the configured matrix to the largest Order the
    router has actually used (observed in .UV/.A traffic), then reload.

    Grow-only — shrinking can't be proven safe from observed traffic alone.
    Does nothing when the configured size already covers everything seen.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:resize"

    def __init__(self, entry: ConfigEntry, client) -> None:
        self._entry = entry
        self._client = client
        self._attr_unique_id = f"{entry.entry_id}_resize_detected"

    @property
    def name(self) -> str:
        return "Resize to Detected"

    @property
    def device_info(self) -> DeviceInfo:
        return _device_info(self._entry)

    @property
    def available(self) -> bool:
        return self._client._connected  # noqa: SLF001

    async def async_press(self) -> None:
        cur_src, cur_dst = self._client.max_sources, self._client.max_destinations
        new_src = max(cur_src, self._client.max_src_order_seen)
        new_dst = max(cur_dst, self._client.max_dst_order_seen)
        router  = router_display_name(self._entry)
        notif_id = f"evertz_quartz_{self._entry.entry_id}_resize"

        if new_src == cur_src and new_dst == cur_dst:
            _LOGGER.info(
                "[%s] Resize to Detected: no change — configured %d×%d already covers "
                "all seen Orders", router, cur_src, cur_dst,
            )
            await self.hass.services.async_call("persistent_notification", "create", {
                "notification_id": notif_id,
                "title": f"Evertz Quartz [{router}] — Profile Size",
                "message": (
                    f"No resize needed. The configured size "
                    f"(**{cur_src} sources × {cur_dst} destinations**) already covers "
                    "every Order the router has used so far."
                ),
            })
            return

        _LOGGER.info(
            "[%s] Resize to Detected: %d×%d → %d×%d (reloading)",
            router, cur_src, cur_dst, new_src, new_dst,
        )
        await apply_resize(self.hass, self._entry, new_src, new_dst)
        await self.hass.services.async_call("persistent_notification", "create", {
            "notification_id": notif_id,
            "title": f"Evertz Quartz [{router}] — Profile Resized",
            "message": (
                f"Resized from **{cur_src}×{cur_dst}** to "
                f"**{new_src} sources × {new_dst} destinations**, based on the largest "
                "Order numbers the router has used. The integration is reloading to "
                "create the new entities.\n\nNew slots show generic names "
                "(*Source N* / *Destination N*) until you re-import the profile CSV."
            ),
        })


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
        # Fire mnemonic callback so all entities redraw with fallback names
        for cb in self.hass.data[DOMAIN][self._entry.entry_id].get("mnemonic_listeners", []):
            self.hass.loop.call_soon_threadsafe(cb)

        # Try querying router for names now that csv_loaded is False
        if self._client._connected:  # noqa: SLF001
            await self._client.query_all_mnemonics()
