"""Config flow for Evertz Quartz integration."""

from __future__ import annotations

import asyncio
import logging

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_LEVELS,
    CONF_MAX_DESTINATIONS,
    CONF_MAX_SOURCES,
    DEFAULT_LEVELS,
    DEFAULT_MAX_DESTINATIONS,
    DEFAULT_MAX_SOURCES,
    DEFAULT_PORT,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


async def _validate_connection(hass: HomeAssistant, data: dict) -> dict:
    """Try to open a TCP connection to verify host/port are reachable."""
    host = data[CONF_HOST]
    port = data[CONF_PORT]

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=10
        )
        writer.close()
        await writer.wait_closed()
    except asyncio.TimeoutError as err:
        raise CannotConnect from err
    except OSError as err:
        raise CannotConnect from err

    return {"title": f"Evertz Quartz ({host}:{port})"}


class EvertzQuartzConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the config flow for Evertz Quartz."""

    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                info = await _validate_connection(self.hass, user_input)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected exception during config flow")
                errors["base"] = "unknown"
            else:
                return self.async_create_entry(title=info["title"], data=user_input)

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST): str,
                vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
                vol.Required(CONF_MAX_SOURCES, default=DEFAULT_MAX_SOURCES): int,
                vol.Required(CONF_MAX_DESTINATIONS, default=DEFAULT_MAX_DESTINATIONS): int,
                vol.Required(CONF_LEVELS, default=DEFAULT_LEVELS): str,
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )


class CannotConnect(Exception):
    """Error to indicate we cannot connect."""


    @staticmethod
    def async_get_options_flow(config_entry):
        """Return the options flow handler."""
        from .options_flow import EvertzQuartzOptionsFlow
        return EvertzQuartzOptionsFlow(config_entry)
