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

from .const import CONF_MAX_DESTINATIONS, DEFAULT_MAX_DESTINATIONS, DOMAIN
from .helpers import effective, router_display_name

_LOGGER = logging.getLogger(__name__)


def _device_info(entry: ConfigEntry):
    from .helpers import device_info as _di
    return _di(entry)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up binary sensors — connection status and profile mismatch."""
    async_add_entities([
        QuartzConnectedSensor(hass, entry),
        QuartzProfileMismatchSensor(hass, entry),
    ])


class QuartzConnectedSensor(BinarySensorEntity):
    """
    Binary sensor showing live TCP connection status to the router.
    ON = connected, OFF = disconnected.
    Updates immediately when the connection state changes.
    """

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_connected"

    @property
    def name(self) -> str:
        return "Connected"

    @property
    def device_info(self):
        return _device_info(self._entry)

    @property
    def is_on(self) -> bool:
        client = (
            self._hass.data.get(DOMAIN, {})
            .get(self._entry.entry_id, {})
            .get("client")
        )
        return bool(client and client._connected)  # noqa: SLF001

    @property
    def extra_state_attributes(self) -> dict:
        client = (
            self._hass.data.get(DOMAIN, {})
            .get(self._entry.entry_id, {})
            .get("client")
        )
        if not client:
            return {}
        import time
        return {
            "host":             client.host,
            "port":             client.port,
            "reconnect_count":  client.stats.reconnect_count,
            "last_connected":   (
                time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(client.stats.connect_time))
                if client.stats.connect_time else None
            ),
            "last_disconnected": (
                time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(client.stats.disconnect_time))
                if client.stats.disconnect_time else None
            ),
        }

    async def async_added_to_hass(self) -> None:
        entry_data = self._hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        if "connection_listeners" in entry_data:
            entry_data["connection_listeners"].append(self._on_connection_change)

    @callback
    def _on_connection_change(self) -> None:
        self.async_write_ha_state()


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
        """ON when the router used an Order beyond the configured size, or when
        fewer destinations exist than configured (over-provisioned placeholder)."""
        entry_data = self._hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        if entry_data.get("mismatch_orders"):
            return True
        client = entry_data.get("client")
        if client is not None:
            configured = effective(self._entry, CONF_MAX_DESTINATIONS, DEFAULT_MAX_DESTINATIONS)
            detected = client.max_dst_order_seen
            if 1 <= detected < configured:
                return True
        return False

    @property
    def extra_state_attributes(self) -> dict:
        entry_data = self._hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        client = entry_data.get("client")
        orders = entry_data.get("mismatch_orders", set())

        attrs: dict = {
            "out_of_range_source_orders": sorted(o for k, o in orders if k == "src"),
            "out_of_range_destination_orders": sorted(o for k, o in orders if k == "dst"),
        }
        over_provisioned = False
        if client is not None:
            cfg_src,  cfg_dst  = client.max_sources, client.max_destinations
            seen_src, seen_dst = client.max_src_order_seen, client.max_dst_order_seen
            over_provisioned = bool(1 <= seen_dst < cfg_dst)
            attrs.update({
                "configured_max_sources":      cfg_src,
                "configured_max_destinations": cfg_dst,
                # Destinations the controller acknowledged via .I/.A interrogation
                # (0 = could not detect — e.g. controller ignores .I)
                "detected_destinations":       seen_dst,
                "destinations_over_provisioned": over_provisioned,
                "suggested_max_destinations":  seen_dst if seen_dst >= 1 else cfg_dst,
                # Sources can't be enumerated over the protocol — grow-only hint
                "detected_min_sources":        seen_src,
                "suggested_max_sources":       max(cfg_src, seen_src),
            })
        action = bool(orders) or over_provisioned
        attrs["action_required"] = action
        if action:
            attrs["resolution"] = (
                "Open Settings → Devices & Services → Evertz Quartz → Configure → "
                "Update Profile and set Max Destinations to the detected value "
                "(or upload the profile CSV)."
            )
        return attrs

    async def async_added_to_hass(self) -> None:
        """Update on mismatch events and on route updates (which carry the
        detected destination count via .A/.UV replies)."""
        entry_data = self._hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        if "mismatch_listeners" in entry_data:
            entry_data["mismatch_listeners"].append(self._on_mismatch)
        if "route_listeners" in entry_data:
            entry_data["route_listeners"].append(self._on_route)

    @callback
    def _on_mismatch(self) -> None:
        self.async_write_ha_state()

    @callback
    def _on_route(self, dest_order: int, src_order: int, levels: str) -> None:
        # A new .A/.UV reply can change the detected destination count
        self.async_write_ha_state()
