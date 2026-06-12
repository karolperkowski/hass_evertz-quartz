"""Sensor platform for Evertz Quartz — connection and profile status."""

from __future__ import annotations

import logging
import time

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_CSV_LOADED, CONF_MAX_DESTINATIONS, CONF_MAX_SOURCES, DOMAIN
from .helpers import readonly_destinations

_LOGGER = logging.getLogger(__name__)


def _device_info(entry: ConfigEntry):
    from .helpers import device_info as _di
    return _di(entry)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up diagnostic sensors + read-only destination source sensors."""
    client = hass.data[DOMAIN][entry.entry_id]["client"]
    entities: list[SensorEntity] = [
        QuartzLastConnectedSensor(hass, entry),
        QuartzProfileSummarySensor(hass, entry),
    ]
    entities.extend(
        QuartzDestinationSourceSensor(hass, entry, client, order)
        for order in sorted(readonly_destinations(entry))
    )
    async_add_entities(entities)


class QuartzDestinationSourceSensor(SensorEntity):
    """
    Read-only view of a destination's current source.

    Created for destinations marked read-only in the Configure panel.
    State = the CSV name of the routed source — display only, no control.
    All numbers are Order indices (MAGNUM numbering).
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_icon = "mdi:video-input-hdmi"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, client, order: int) -> None:
        self._hass   = hass
        self._entry  = entry
        self._client = client
        self._order  = order   # destination Order index (MAGNUM numbering)
        self._attr_unique_id = f"{entry.entry_id}_dest_source_{order}"

    @property
    def name(self) -> str:
        dest_name = self._client.destination_names.get(self._order)
        return f"{dest_name or f'Destination {self._order}'} Source"

    @property
    def device_info(self):
        return _device_info(self._entry)

    @property
    def native_value(self) -> str | None:
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
        dest_ns   = self._client.destination_namespaces.get(self._order)
        src_ns    = self._client.source_namespaces.get(src_order) if src_order else None
        lock_val  = self._client.locks.get(self._order, 0)
        return {
            "router":            self._entry.data.get("router_name") or self._entry.data.get("host", ""),
            "destination_order": self._order,
            "destination_name":  self._client.destination_names.get(self._order)
                                 or f"Destination {self._order}",
            "destination_namespace": dest_ns,
            "source_order":      src_order,
            "source_namespace":  src_ns,
            "locked":            lock_val > 0,
            "read_only":         True,
        }

    async def async_added_to_hass(self) -> None:
        entry_data = self._hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        if "route_listeners" in entry_data:
            entry_data["route_listeners"].append(self._on_route_update)
        if "mnemonic_listeners" in entry_data:
            entry_data["mnemonic_listeners"].append(self._on_mnemonic_update)

    @callback
    def _on_route_update(self, dest_order: int, src_order: int, levels: str) -> None:
        if dest_order == self._order:
            self.async_write_ha_state()

    @callback
    def _on_mnemonic_update(self) -> None:
        self.async_write_ha_state()


class QuartzLastConnectedSensor(SensorEntity):
    """
    Sensor showing the timestamp of the last successful TCP connection.
    Visible on the device card — useful for spotting intermittent drops.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_should_poll = False
    _attr_icon = "mdi:lan-connect"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_last_connected"

    @property
    def name(self) -> str:
        return "Last Connected"

    @property
    def device_info(self):
        return _device_info(self._entry)

    @property
    def native_value(self):
        """Return last connect time as a datetime (HA handles formatting)."""
        from datetime import datetime, timezone
        client = (
            self._hass.data.get(DOMAIN, {})
            .get(self._entry.entry_id, {})
            .get("client")
        )
        if not client or not client.stats.connect_time:
            return None
        return datetime.fromtimestamp(client.stats.connect_time, tz=timezone.utc)

    @property
    def extra_state_attributes(self) -> dict:
        client = (
            self._hass.data.get(DOMAIN, {})
            .get(self._entry.entry_id, {})
            .get("client")
        )
        if not client:
            return {}
        from datetime import datetime, timezone
        attrs: dict = {
            "reconnect_count": client.stats.reconnect_count,
        }
        if client.stats.disconnect_time:
            attrs["last_disconnected"] = datetime.fromtimestamp(
                client.stats.disconnect_time, tz=timezone.utc
            ).isoformat()
        if client.stats.last_rx_time:
            attrs["last_message_received"] = datetime.fromtimestamp(
                client.stats.last_rx_time, tz=timezone.utc
            ).isoformat()
        return attrs

    async def async_added_to_hass(self) -> None:
        entry_data = self._hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        if "connection_listeners" in entry_data:
            entry_data["connection_listeners"].append(self._on_connection_change)

    @callback
    def _on_connection_change(self) -> None:
        self.async_write_ha_state()


class QuartzProfileSummarySensor(SensorEntity):
    """
    Sensor showing the current router profile dimensions and CSV status.
    State = source count. Attributes contain full detail.
    Visible on the device card without opening Configure.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False
    _attr_icon = "mdi:router-network"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = "sources"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_profile_summary"

    @property
    def name(self) -> str:
        return "Profile"

    @property
    def device_info(self):
        return _device_info(self._entry)

    @property
    def native_value(self) -> int:
        """State = max sources (easy to see at a glance on the device card)."""
        client = (
            self._hass.data.get(DOMAIN, {})
            .get(self._entry.entry_id, {})
            .get("client")
        )
        if client:
            return client.max_sources
        return self._entry.data.get(CONF_MAX_SOURCES, 0)

    @property
    def extra_state_attributes(self) -> dict:
        client = (
            self._hass.data.get(DOMAIN, {})
            .get(self._entry.entry_id, {})
            .get("client")
        )
        entry = self._entry
        if client:
            max_src = client.max_sources
            max_dst = client.max_destinations
            csv_loaded = client.csv_loaded
            src_names = len(client.source_names)
            dst_names = len(client.destination_names)
        else:
            max_src = entry.data.get(CONF_MAX_SOURCES, 0)
            max_dst = entry.data.get(CONF_MAX_DESTINATIONS, 0)
            csv_loaded = entry.data.get(CONF_CSV_LOADED, False)
            src_names = len(entry.data.get("source_names", {}))
            dst_names = len(entry.data.get("destination_names", {}))

        return {
            "max_sources":         max_src,
            "max_destinations":    max_dst,
            "csv_loaded":          csv_loaded,
            "source_names_loaded": src_names,
            "destination_names_loaded": dst_names,
            "host":                entry.data.get("host", ""),
            "port":                entry.data.get("port", ""),
        }
