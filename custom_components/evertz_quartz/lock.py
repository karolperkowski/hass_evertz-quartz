"""Lock platform for Evertz Quartz — destination lock/unlock via .BL / .BU commands."""

from __future__ import annotations

import logging

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_MAX_DESTINATIONS, DEFAULT_MAX_DESTINATIONS, DOMAIN
from .helpers import device_info, effective, router_display_name

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one lock entity per destination."""
    client = hass.data[DOMAIN][entry.entry_id]["client"]
    max_dst = effective(entry, CONF_MAX_DESTINATIONS, DEFAULT_MAX_DESTINATIONS)
    async_add_entities([
        QuartzDestinationLock(hass, entry, client, order)
        for order in range(1, max_dst + 1)
    ])


class QuartzDestinationLock(LockEntity):
    """
    Lock entity for a single router destination.

    Locked   = destination is protected — routes blocked on all panels.
    Unlocked = destination is free to route.

    Protocol:
      Lock:   TX .BL{dest}
      Unlock: TX .BU{dest}
      Query:  TX .BI{dest}
      Update: RX .BA{dest},{value}  (255=locked, 0=unlocked)
    """

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client,
        order: int,
    ) -> None:
        self._hass   = hass
        self._entry  = entry
        self._client = client
        self._order  = order
        self._attr_unique_id = f"{entry.entry_id}_lock_{order}"

    @property
    def name(self) -> str:
        dest_name = self._client.destination_names.get(self._order)
        return f"{dest_name or f'Destination {self._order}'} Lock"

    @property
    def device_info(self):
        return device_info(self._entry)

    @property
    def is_locked(self) -> bool:
        """True when lock value > 0."""
        return self._client.locks.get(self._order, 0) > 0

    @property
    def is_locking(self) -> bool:
        return False

    @property
    def is_unlocking(self) -> bool:
        return False

    @property
    def available(self) -> bool:
        return self._client._connected  # noqa: SLF001

    @property
    def extra_state_attributes(self) -> dict:
        lock_val  = self._client.locks.get(self._order, 0)
        dest_name = self._client.destination_names.get(self._order)
        dest_ns   = self._client.destination_namespaces.get(self._order)
        # AN65 lock value semantics:
        #   0       = unlocked
        #   1-254   = locked by a hardware panel at Q-link address (n-1)
        #             .BU may not clear this — must be released from the panel
        #   255     = unprotected software lock (set via .BL command)
        if lock_val == 0:
            lock_type = "unlocked"
        elif lock_val == 255:
            lock_type = "software"        # set by .BL, clearable by .BU
        else:
            lock_type = f"panel:{lock_val}"  # hardware panel lock, Q-link addr {lock_val-1}

        return {
            "destination_order":     self._order,
            "destination_name":      dest_name or f"Destination {self._order}",
            "destination_namespace": dest_ns,
            "lock_value":            lock_val,
            "lock_type":             lock_type,
            "panel_clearable":       lock_val == 255,  # only software locks can be cleared remotely
        }

    async def async_lock(self, **kwargs) -> None:
        """Lock this destination via .BL command."""
        rname     = router_display_name(self._entry)
        dest_name = self._client.destination_names.get(self._order, f"Dest {self._order}")
        _LOGGER.info("[%s] Locking destination %s (Order %d)", rname, dest_name, self._order)
        await self._client.lock_destination(self._order)

    async def async_unlock(self, **kwargs) -> None:
        """
        Unlock this destination via .BU command.
        Note: if lock_value is 1-254, this is a panel lock set by a hardware Q-link panel.
        .BU may not clear it — the panel itself must release the lock.
        """
        rname     = router_display_name(self._entry)
        dest_name = self._client.destination_names.get(self._order, f"Dest {self._order}")
        lock_val  = self._client.locks.get(self._order, 0)
        if 0 < lock_val < 255:
            _LOGGER.warning(
                "[%s] Unlock attempted on %s (Order %d) but lock_value=%d indicates "
                "a hardware panel lock (Q-link address %d). .BU may not clear it — "
                "the panel at that address must release the lock.",
                rname, dest_name, self._order, lock_val, lock_val - 1,
            )
        else:
            _LOGGER.info("[%s] Unlocking destination %s (Order %d)", rname, dest_name, self._order)
        await self._client.unlock_destination(self._order)

    async def async_added_to_hass(self) -> None:
        """Register for lock state updates."""
        entry_data = self._hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {})
        if "lock_listeners" in entry_data:
            entry_data["lock_listeners"].append(self._on_lock_update)

    @callback
    def _on_lock_update(self, dest_order: int, lock_value: int) -> None:
        if dest_order == self._order:
            self.async_write_ha_state()
