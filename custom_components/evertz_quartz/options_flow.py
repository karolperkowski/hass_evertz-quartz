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
from .csv_parser import parse_csv

_LOGGER = logging.getLogger(__name__)
_MAX_SIZE = 1024
CONF_CSV_PROFILE = "csv_profile"


def _effective(entry: ConfigEntry, key: str, default):
    if key in entry.options:
        return entry.options[key]
    return entry.data.get(key, default)


class EvertzQuartzOptionsFlow(OptionsFlow):
    """Handle the Configure panel on the integration card."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry
        # Holds counts resolved from a valid CSV paste so they survive
        # form re-display on error in other fields
        self._csv_sources: int | None = None
        self._csv_destinations: int | None = None
        self._csv_source_names: dict[int, str] = {}
        self._csv_dest_names: dict[int, str] = {}
        self._csv_format: str = ""

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        errors: dict[str, str] = {}
        csv_summary: str = ""

        if user_input is not None:
            csv_text = user_input.pop(CONF_CSV_PROFILE, "").strip()

            # Parse CSV if provided
            if csv_text:
                result = parse_csv(csv_text)
                if result is None:
                    errors[CONF_CSV_PROFILE] = "csv_parse_error"
                else:
                    if result.max_sources > 0:
                        self._csv_sources = result.max_sources
                        self._csv_source_names = result.source_names
                        user_input[CONF_MAX_SOURCES] = result.max_sources
                    if result.max_destinations > 0:
                        self._csv_destinations = result.max_destinations
                        self._csv_dest_names = result.destination_names
                        user_input[CONF_MAX_DESTINATIONS] = result.max_destinations
                    self._csv_format = result.format_detected
                    _LOGGER.info(
                        "CSV parsed (%s): %d sources, %d destinations, warnings: %s",
                        result.format_detected,
                        result.max_sources,
                        result.max_destinations,
                        result.warnings or "none",
                    )

            if not errors:
                opts = user_input
                client = (
                    self.hass.data.get(DOMAIN, {})
                    .get(self._entry.entry_id, {})
                    .get("client")
                )

                old_max_src = _effective(self._entry, CONF_MAX_SOURCES,     DEFAULT_MAX_SOURCES)
                old_max_dst = _effective(self._entry, CONF_MAX_DESTINATIONS, DEFAULT_MAX_DESTINATIONS)
                old_levels  = _effective(self._entry, CONF_LEVELS,           DEFAULT_LEVELS)

                new_max_src = opts[CONF_MAX_SOURCES]
                new_max_dst = opts[CONF_MAX_DESTINATIONS]
                new_levels  = opts[CONF_LEVELS]

                # Apply debug/connection options live
                if client:
                    client.update_options(
                        verbose_logging=opts.get(CONF_VERBOSE_LOGGING),
                        reconnect_delay=opts.get(CONF_RECONNECT_DELAY),
                        connect_timeout=opts.get(CONF_CONNECT_TIMEOUT),
                    )

                # Apply levels live
                if new_levels != old_levels and client:
                    client.levels = new_levels
                    _LOGGER.info("Levels updated live to: %s", new_levels)
                    self.hass.async_create_task(client.query_all_mnemonics())

                # Apply max_sources live
                if new_max_src != old_max_src and client:
                    client.max_sources = new_max_src
                    _LOGGER.info("Max sources updated live to: %d", new_max_src)
                    # If we have CSV names, push them into the client immediately
                    if self._csv_source_names:
                        client.source_names.update(self._csv_source_names)
                    self.hass.async_create_task(client.query_all_mnemonics())
                    self.hass.async_create_task(client.query_all_routes())

                # Push CSV destination names live if destinations didn't change
                # (if they did, the reload below will pick them up on reconnect)
                if self._csv_dest_names and client and new_max_dst == old_max_dst:
                    client.destination_names.update(self._csv_dest_names)
                    # Trigger mnemonic callback so entities redraw
                    for cb in self.hass.data.get(DOMAIN, {}).get(
                        self._entry.entry_id, {}
                    ).get("mnemonic_listeners", []):
                        self.hass.loop.call_soon_threadsafe(cb)

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

        # Pre-fill form with effective current values
        cur_max_src = self._csv_sources  or _effective(self._entry, CONF_MAX_SOURCES,     DEFAULT_MAX_SOURCES)
        cur_max_dst = self._csv_destinations or _effective(self._entry, CONF_MAX_DESTINATIONS, DEFAULT_MAX_DESTINATIONS)
        cur_levels  = _effective(self._entry, CONF_LEVELS,           DEFAULT_LEVELS)
        cur_verbose = _effective(self._entry, CONF_VERBOSE_LOGGING,  DEFAULT_VERBOSE_LOGGING)
        cur_recon   = _effective(self._entry, CONF_RECONNECT_DELAY,  DEFAULT_RECONNECT_DELAY)
        cur_timeout = _effective(self._entry, CONF_CONNECT_TIMEOUT,  DEFAULT_CONNECT_TIMEOUT)

        schema = vol.Schema({
            vol.Required(CONF_MAX_SOURCES,      default=cur_max_src):  vol.All(int, vol.Range(min=1, max=_MAX_SIZE)),
            vol.Required(CONF_MAX_DESTINATIONS, default=cur_max_dst):  vol.All(int, vol.Range(min=1, max=_MAX_SIZE)),
            vol.Required(CONF_LEVELS,           default=cur_levels):   str,
            vol.Required(CONF_VERBOSE_LOGGING,  default=cur_verbose):  bool,
            vol.Required(CONF_RECONNECT_DELAY,  default=cur_recon):    vol.All(int, vol.Range(min=1, max=300)),
            vol.Required(CONF_CONNECT_TIMEOUT,  default=cur_timeout):  vol.All(int, vol.Range(min=3, max=60)),
            vol.Optional(CONF_CSV_PROFILE,      default=""):           str,
        })

        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "host":         self._entry.data.get("host", ""),
                "current_size": f"{cur_max_src} sources × {cur_max_dst} destinations",
                "csv_format":   f"Last parsed: {self._csv_format}" if self._csv_format else "",
            },
        )
