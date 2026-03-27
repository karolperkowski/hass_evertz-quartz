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

_LOGGER = logging.getLogger(__name__)


def _device_info(entry: ConfigEntry):
    from .helpers import device_info as _di
    return _di(entry)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up diagnostic sensors."""
    async_add_entities([
        QuartzLastConnectedSensor(hass, entry),
        QuartzProfileSummarySensor(hass, entry),
    ])


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
