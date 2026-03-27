"""Config flow for Evertz Quartz — two steps: connect then profile."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    FileSelector,
    FileSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_LEVELS,
    CONF_MAX_DESTINATIONS,
    CONF_MAX_SOURCES,
    CONF_NAME,
    DEFAULT_LEVELS,
    DEFAULT_MAX_DESTINATIONS,
    DEFAULT_MAX_SOURCES,
    DEFAULT_PORT,
    DOMAIN,
)
from .csv_parser import parse_csv

_LOGGER = logging.getLogger(__name__)

_MAX_SIZE = 2048
CONF_CSV_UPLOAD = "csv_upload"


async def _validate_connection(host: str, port: int) -> None:
    """Open and immediately close a TCP connection to verify reachability."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=10
        )
        writer.close()
        await writer.wait_closed()
    except (asyncio.TimeoutError, OSError) as err:
        raise CannotConnect from err


def _parse_uploaded_csv(hass, upload_id: str) -> tuple[dict, list[str]]:
    """
    Read the uploaded file, parse it, return (fields_to_override, warnings).
    fields_to_override is a partial dict with any of:
        max_sources, max_destinations, source_names, destination_names
    Only sides that have data in the CSV are included — an SRC-only export
    will never touch destination counts.
    """
    try:
        with process_uploaded_file(hass, upload_id) as file_path:
            text = Path(file_path).read_text(encoding="utf-8", errors="replace")
    except Exception as err:  # noqa: BLE001
        return {}, [f"Could not read uploaded file: {err}"]

    result = parse_csv(text)
    if result is None:
        return {}, ["File could not be parsed — check the format and try again."]

    overrides: dict = {}
    if result.max_sources > 0:
        overrides[CONF_MAX_SOURCES] = result.max_sources
        overrides["source_names"] = result.source_names
    if result.max_destinations > 0:
        overrides[CONF_MAX_DESTINATIONS] = result.max_destinations
        overrides["destination_names"] = result.destination_names

    warnings = list(result.warnings)
    if result.hidden_sources or result.hidden_destinations:
        warnings.append(
            f"{result.hidden_sources} hidden source(s) and "
            f"{result.hidden_destinations} hidden destination(s) "
            f"present in CSV but excluded from profile."
        )

    _LOGGER.info(
        "CSV uploaded (%s): %s",
        result.format_detected,
        result.summary,
    )
    return overrides, warnings


class EvertzQuartzConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Two-step config flow: connection details → profile / CSV upload."""

    VERSION = 1

    def __init__(self) -> None:
        self._host: str = ""
        self._port: int = DEFAULT_PORT
        self._router_name: str = ""
        # Profile data — populated from CSV or left at defaults
        self._max_sources: int = DEFAULT_MAX_SOURCES
        self._max_destinations: int = DEFAULT_MAX_DESTINATIONS
        self._levels: str = DEFAULT_LEVELS
        self._source_names: dict[int, str] = {}
        self._destination_names: dict[int, str] = {}
        self._csv_warnings: list[str] = []

    # ── Step 1: connection ────────────────────────────────────────────────

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        """Host, port, optional router name.  Validates TCP connectivity."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = user_input[CONF_PORT]
            name = user_input.get(CONF_NAME, "").strip()

            try:
                await _validate_connection(host, port)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            else:
                self._host = host
                self._port = port
                self._router_name = name or host   # fall back to IP
                return await self.async_step_profile()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_HOST): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT)
                ),
                vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
                vol.Optional(CONF_NAME, default=""): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT)
                ),
            }),
            errors=errors,
        )

    # ── Step 2: profile / CSV ────────────────────────────────────────────

    async def async_step_profile(self, user_input: dict | None = None) -> FlowResult:
        """Max sources, destinations, levels, optional CSV file upload."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Process CSV upload if provided
            upload_id = user_input.get(CONF_CSV_UPLOAD)
            if upload_id:
                overrides, warnings = await self.hass.async_add_executor_job(
                    _parse_uploaded_csv, self.hass, upload_id
                )
                if warnings and not overrides:
                    errors[CONF_CSV_UPLOAD] = "csv_parse_error"
                    self._csv_warnings = warnings
                else:
                    self._csv_warnings = warnings
                    if CONF_MAX_SOURCES in overrides:
                        self._max_sources = overrides[CONF_MAX_SOURCES]
                        self._source_names = overrides.get("source_names", {})
                    if CONF_MAX_DESTINATIONS in overrides:
                        self._max_destinations = overrides[CONF_MAX_DESTINATIONS]
                        self._destination_names = overrides.get("destination_names", {})
                    # Re-show form with populated values so user can review
                    if not errors:
                        return await self.async_step_profile()

            if not errors:
                data = {
                    CONF_HOST: self._host,
                    CONF_PORT: self._port,
                    CONF_NAME: self._router_name,
                    CONF_MAX_SOURCES: user_input.get(CONF_MAX_SOURCES, self._max_sources),
                    CONF_MAX_DESTINATIONS: user_input.get(CONF_MAX_DESTINATIONS, self._max_destinations),
                    CONF_LEVELS: user_input.get(CONF_LEVELS, self._levels),
                    # Store names in data so they survive reloads
                    "source_names": self._source_names,
                    "destination_names": self._destination_names,
                }
                title = self._router_name
                return self.async_create_entry(title=title, data=data)

        return self.async_show_form(
            step_id="profile",
            data_schema=vol.Schema({
                vol.Required(CONF_MAX_SOURCES,      default=self._max_sources):      vol.All(int, vol.Range(min=1, max=_MAX_SIZE)),
                vol.Required(CONF_MAX_DESTINATIONS, default=self._max_destinations): vol.All(int, vol.Range(min=1, max=_MAX_SIZE)),
                vol.Required(CONF_LEVELS,           default=self._levels):           str,
                vol.Optional(CONF_CSV_UPLOAD): FileSelector(
                    FileSelectorConfig(accept=".csv,text/csv")
                ),
            }),
            errors=errors,
            description_placeholders={
                "router_name": self._router_name,
                "csv_warnings": "; ".join(self._csv_warnings) if self._csv_warnings else "",
            },
        )

    @staticmethod
    def async_get_options_flow(config_entry):
        from .options_flow import EvertzQuartzOptionsFlow
        return EvertzQuartzOptionsFlow(config_entry)


class CannotConnect(Exception):
    """Error indicating TCP connection failed."""
