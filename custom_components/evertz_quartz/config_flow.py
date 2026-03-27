"""Config flow for Evertz Quartz integration."""

from __future__ import annotations

import asyncio
import logging

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
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
from .csv_parser import parse_csv

_LOGGER = logging.getLogger(__name__)

_PROBE_MAX = 1024
_PROBE_TIMEOUT = 1.5
_DETECT_TIMEOUT = 30

CONF_CSV_PROFILE = "csv_profile"


async def _validate_connection(host: str, port: int) -> None:
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=10
        )
        writer.close()
        await writer.wait_closed()
    except (asyncio.TimeoutError, OSError) as err:
        raise CannotConnect from err


async def _probe_router_size(host: str, port: int) -> tuple[int, int]:
    """Binary-search probe for max sources/destinations. Returns defaults on failure."""
    _LOGGER.debug("Probing router size at %s:%d", host, port)
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=10
        )
    except (asyncio.TimeoutError, OSError) as err:
        _LOGGER.warning("Could not connect for probing: %s", err)
        return DEFAULT_MAX_SOURCES, DEFAULT_MAX_DESTINATIONS

    async def send_cmd(cmd: str) -> str | None:
        try:
            writer.write(cmd.encode())
            await writer.drain()
            for _ in range(3):
                raw = await asyncio.wait_for(reader.readline(), timeout=_PROBE_TIMEOUT)
                line = raw.decode(errors="replace").strip()
                if not line or line.startswith(".UV") or line.startswith(".P"):
                    continue
                return line
        except (asyncio.TimeoutError, OSError):
            pass
        return None

    def is_valid(resp: str | None, prefix: str) -> bool:
        if not resp or resp.startswith(".E"):
            return False
        return resp.upper().startswith(prefix.upper())

    async def bisect(cmd_prefix: str) -> int:
        lo, hi, result = 1, _PROBE_MAX, 0
        while lo <= hi:
            mid = (lo + hi) // 2
            resp = await send_cmd(f"{cmd_prefix}{mid}\r")
            if is_valid(resp, cmd_prefix):
                result = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return result or DEFAULT_MAX_SOURCES

    try:
        max_dsts = await asyncio.wait_for(bisect(".RD"), timeout=_DETECT_TIMEOUT)
        max_srcs = await asyncio.wait_for(bisect(".RT"), timeout=_DETECT_TIMEOUT)
    except asyncio.TimeoutError:
        _LOGGER.warning("Probe timed out — using defaults")
        max_srcs, max_dsts = DEFAULT_MAX_SOURCES, DEFAULT_MAX_DESTINATIONS
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass

    _LOGGER.info("Probed router: %d sources, %d destinations", max_srcs, max_dsts)
    return max_srcs, max_dsts


class EvertzQuartzConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the config flow for Evertz Quartz."""

    VERSION = 1

    def __init__(self) -> None:
        self._host: str = ""
        self._port: int = DEFAULT_PORT
        self._detected_sources: int = DEFAULT_MAX_SOURCES
        self._detected_destinations: int = DEFAULT_MAX_DESTINATIONS
        self._csv_source_names: dict[int, str] = {}
        self._csv_dest_names: dict[int, str] = {}

    # ── Step 1: host + port ───────────────────────────────────────────────

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                await _validate_connection(user_input[CONF_HOST], user_input[CONF_PORT])
            except CannotConnect:
                errors["base"] = "cannot_connect"
            else:
                self._host = user_input[CONF_HOST]
                self._port = user_input[CONF_PORT]
                return await self.async_step_detect()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_HOST): str,
                vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
            }),
            errors=errors,
        )

    # ── Step 2: auto-probe + optional CSV ────────────────────────────────

    async def async_step_detect(self, user_input: dict | None = None) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is None:
            # First arrival — run the probe
            srcs, dsts = await _probe_router_size(self._host, self._port)
            self._detected_sources = srcs
            self._detected_destinations = dsts

        if user_input is not None:
            csv_text = user_input.get(CONF_CSV_PROFILE, "").strip()

            # If CSV was provided, try to parse it
            if csv_text:
                result = parse_csv(csv_text)
                if result is None:
                    errors[CONF_CSV_PROFILE] = "csv_parse_error"
                else:
                    # CSV wins over probe — use its counts, store names for later
                    if result.max_sources > 0:
                        self._detected_sources = result.max_sources
                        self._csv_source_names = result.source_names
                    if result.max_destinations > 0:
                        self._detected_destinations = result.max_destinations
                        self._csv_dest_names = result.destination_names
                    _LOGGER.info(
                        "CSV parsed (%s): %d sources, %d destinations",
                        result.format_detected,
                        result.max_sources,
                        result.max_destinations,
                    )

            if not errors:
                data = {
                    CONF_HOST: self._host,
                    CONF_PORT: self._port,
                    CONF_MAX_SOURCES: user_input[CONF_MAX_SOURCES],
                    CONF_MAX_DESTINATIONS: user_input[CONF_MAX_DESTINATIONS],
                    CONF_LEVELS: user_input[CONF_LEVELS],
                }
                title = (
                    f"Evertz Quartz ({self._host}:{self._port})"
                    f" — {data[CONF_MAX_SOURCES]}×{data[CONF_MAX_DESTINATIONS]}"
                )
                return self.async_create_entry(title=title, data=data)

        return self.async_show_form(
            step_id="detect",
            data_schema=vol.Schema({
                vol.Required(CONF_MAX_SOURCES,      default=self._detected_sources):      vol.All(int, vol.Range(min=1, max=_PROBE_MAX)),
                vol.Required(CONF_MAX_DESTINATIONS, default=self._detected_destinations): vol.All(int, vol.Range(min=1, max=_PROBE_MAX)),
                vol.Required(CONF_LEVELS,           default=DEFAULT_LEVELS):              str,
                vol.Optional(CONF_CSV_PROFILE,      default=""):                          str,
            }),
            errors=errors,
            description_placeholders={
                "host":                   self._host,
                "port":                   str(self._port),
                "detected_sources":       str(self._detected_sources),
                "detected_destinations":  str(self._detected_destinations),
            },
        )

    @staticmethod
    def async_get_options_flow(config_entry):
        from .options_flow import EvertzQuartzOptionsFlow
        return EvertzQuartzOptionsFlow(config_entry)


class CannotConnect(Exception):
    """Error to indicate we cannot connect."""
