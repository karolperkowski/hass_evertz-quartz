"""Options flow for Evertz Quartz — lets users change debug settings without re-setup."""

from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_CONNECT_TIMEOUT,
    CONF_RECONNECT_DELAY,
    CONF_VERBOSE_LOGGING,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_RECONNECT_DELAY,
    DEFAULT_VERBOSE_LOGGING,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class EvertzQuartzOptionsFlow(OptionsFlow):
    """Handle the options flow (Configure button on the integration card)."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        """Show the options form."""
        if user_input is not None:
            # Push new values to the live client immediately — no restart needed
            client = self.hass.data.get(DOMAIN, {}).get(self._entry.entry_id, {}).get("client")
            if client:
                client.update_options(
                    verbose_logging=user_input.get(CONF_VERBOSE_LOGGING),
                    reconnect_delay=user_input.get(CONF_RECONNECT_DELAY),
                    connect_timeout=user_input.get(CONF_CONNECT_TIMEOUT),
                )
                _LOGGER.info("Evertz Quartz options updated: %s", user_input)

            return self.async_create_entry(title="", data=user_input)

        # Pre-fill with current option values (or defaults)
        current = self._entry.options
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_VERBOSE_LOGGING,
                    default=current.get(CONF_VERBOSE_LOGGING, DEFAULT_VERBOSE_LOGGING),
                ): bool,
                vol.Required(
                    CONF_RECONNECT_DELAY,
                    default=current.get(CONF_RECONNECT_DELAY, DEFAULT_RECONNECT_DELAY),
                ): vol.All(int, vol.Range(min=1, max=300)),
                vol.Required(
                    CONF_CONNECT_TIMEOUT,
                    default=current.get(CONF_CONNECT_TIMEOUT, DEFAULT_CONNECT_TIMEOUT),
                ): vol.All(int, vol.Range(min=3, max=60)),
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            description_placeholders={
                "host": self._entry.data.get("host", ""),
            },
        )
