"""Options flow for Evertz Quartz — runtime Configure panel."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, OptionsFlow
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_CONNECT_TIMEOUT,
    CONF_LEVELS,
    CONF_MAX_DESTINATIONS,
    CONF_MAX_SOURCES,
    CONF_RECONNECT_DELAY,
    CONF_VERBOSE_LOGGING,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_LEVELS,
    DEFAULT_MAX_DESTINATIONS,
    DEFAULT_MAX_SOURCES,
    DEFAULT_RECONNECT_DELAY,
    DEFAULT_VERBOSE_LOGGING,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

# Upper bound for the size inputs — matches the probe cap in config_flow
_MAX_SIZE = 1024


def _effective(entry: ConfigEntry, key: str, default):
    """Return options value if set, otherwise fall back to data, then default."""
    if key in entry.options:
        return entry.options[key]
    return entry.data.get(key, default)


class EvertzQuartzOptionsFlow(OptionsFlow):
    """Handle the Configure panel on the integration card."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        """Show the options form."""
        if user_input is not None:
            opts = user_input
            client = (
                self.hass.data.get(DOMAIN, {})
                .get(self._entry.entry_id, {})
                .get("client")
            )

            # Work out which fields actually changed vs current effective values
            old_max_src  = _effective(self._entry, CONF_MAX_SOURCES,      DEFAULT_MAX_SOURCES)
            old_max_dst  = _effective(self._entry, CONF_MAX_DESTINATIONS,  DEFAULT_MAX_DESTINATIONS)
            old_levels   = _effective(self._entry, CONF_LEVELS,            DEFAULT_LEVELS)

            new_max_src  = opts[CONF_MAX_SOURCES]
            new_max_dst  = opts[CONF_MAX_DESTINATIONS]
            new_levels   = opts[CONF_LEVELS]

            # Apply debug/connection options live immediately
            if client:
                client.update_options(
                    verbose_logging=opts.get(CONF_VERBOSE_LOGGING),
                    reconnect_delay=opts.get(CONF_RECONNECT_DELAY),
                    connect_timeout=opts.get(CONF_CONNECT_TIMEOUT),
                )

            # Apply levels live — update client + re-poll mnemonics so
            # entities refresh their options list
            if new_levels != old_levels and client:
                client.levels = new_levels
                _LOGGER.info("Levels updated live to: %s", new_levels)
                self.hass.async_create_task(client.query_all_mnemonics())

            # Apply max_sources live — update client, re-poll mnemonics &
            # routes so entities grow/shrink their options list
            if new_max_src != old_max_src and client:
                client.max_sources = new_max_src
                _LOGGER.info("Max sources updated live to: %d", new_max_src)
                self.hass.async_create_task(client.query_all_mnemonics())
                self.hass.async_create_task(client.query_all_routes())

            # Save options first, then reload if destinations changed
            result = self.async_create_entry(title="", data=opts)

            if new_max_dst != old_max_dst:
                _LOGGER.info(
                    "Max destinations changed %d → %d — reloading integration",
                    old_max_dst, new_max_dst,
                )
                self.hass.async_create_task(
                    self.hass.config_entries.async_reload(self._entry.entry_id)
                )

            return result

        # Pre-fill the form with current effective values
        cur_max_src = _effective(self._entry, CONF_MAX_SOURCES,      DEFAULT_MAX_SOURCES)
        cur_max_dst = _effective(self._entry, CONF_MAX_DESTINATIONS,  DEFAULT_MAX_DESTINATIONS)
        cur_levels  = _effective(self._entry, CONF_LEVELS,            DEFAULT_LEVELS)
        cur_verbose = _effective(self._entry, CONF_VERBOSE_LOGGING,   DEFAULT_VERBOSE_LOGGING)
        cur_recon   = _effective(self._entry, CONF_RECONNECT_DELAY,   DEFAULT_RECONNECT_DELAY)
        cur_timeout = _effective(self._entry, CONF_CONNECT_TIMEOUT,   DEFAULT_CONNECT_TIMEOUT)

        schema = vol.Schema(
            {
                # --- Router size & routing ---
                vol.Required(CONF_MAX_SOURCES,      default=cur_max_src):  vol.All(int, vol.Range(min=1, max=_MAX_SIZE)),
                vol.Required(CONF_MAX_DESTINATIONS, default=cur_max_dst):  vol.All(int, vol.Range(min=1, max=_MAX_SIZE)),
                vol.Required(CONF_LEVELS,           default=cur_levels):   str,
                # --- Debug & connection ---
                vol.Required(CONF_VERBOSE_LOGGING,  default=cur_verbose):  bool,
                vol.Required(CONF_RECONNECT_DELAY,  default=cur_recon):    vol.All(int, vol.Range(min=1, max=300)),
                vol.Required(CONF_CONNECT_TIMEOUT,  default=cur_timeout):  vol.All(int, vol.Range(min=3, max=60)),
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            description_placeholders={
                "host": self._entry.data.get("host", ""),
                "current_size": (
                    f"{cur_max_src} sources × {cur_max_dst} destinations"
                ),
            },
        )
