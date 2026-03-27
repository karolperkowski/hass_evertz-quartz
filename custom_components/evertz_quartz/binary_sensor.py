"""Binary sensor platform for Evertz Quartz — profile mismatch detection."""

from __future__ import annotations

import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .helpers import router_display_name

_LOGGER = logging.getLogger(__name__)


def _device_info(entry: ConfigEntry):
    from .helpers import device_info as _di
    return _di(entry)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up profile mismatch binary sensor."""
    async_add_entities([QuartzProfileMismatchSensor(hass, entry)])


class QuartzProfileMismatchSensor(BinarySensorEntity):
    """
    Binary sensor that turns ON when the router reports an Order number
    outside the configured max_sources / max_destinations range.

    This indicates the router profile has been expanded or changed and
    HA needs to be updated via Configure → Update Profile.

    Clears (OFF) after a successful Configure save that triggers a reload,
    since the reload starts fresh with the new configured counts.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_should_poll = False
    _attr_icon = "mdi:alert-circle-outline"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_profile_mismatch"

    @property
    def name(self) -> str:
        return "Profile Mismatch"

    @property
    def device_info(self) -> DeviceInfo:
        return _device_info(self._entry)

    @property
    def is_on(self) -> bool:
        """True when any out-of-range Order has been seen this session."""
        entry_data = self._hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        return bool(entry_data.get("mismatch_orders"))

    @property
    def extra_state_attributes(self) -> dict:
        entry_data = self._hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        orders = entry_data.get("mismatch_orders", set())
        if not orders:
            return {"out_of_range_orders": [], "action_required": False}
        src_orders = sorted(o for k, o in orders if k == "src")
        dst_orders = sorted(o for k, o in orders if k == "dst")
        return {
            "out_of_range_source_orders": src_orders,
            "out_of_range_destination_orders": dst_orders,
            "action_required": True,
            "resolution": "Go to Settings → Devices & Services → Evertz Quartz → Configure → Update Profile",
        }

    async def async_added_to_hass(self) -> None:
        """Register as a mismatch listener so we update when the client fires."""
        entry_data = self._hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        if "mismatch_listeners" in entry_data:
            entry_data["mismatch_listeners"].append(self._on_mismatch)

    @callback
    def _on_mismatch(self) -> None:
        self.async_write_ha_state()
