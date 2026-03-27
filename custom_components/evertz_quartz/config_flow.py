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

# Hard upper bound for probing — binary search caps here
_PROBE_MAX = 1024
# Per-command timeout during probing (seconds)
_PROBE_TIMEOUT = 1.5
# Overall detection timeout (seconds)
_DETECT_TIMEOUT = 30


async def _validate_connection(host: str, port: int) -> None:
    """Open and close a TCP connection to verify host/port are reachable."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=10
        )
        writer.close()
        await writer.wait_closed()
    except (asyncio.TimeoutError, OSError) as err:
        raise CannotConnect from err


async def _probe_router_size(
    host: str,
    port: int,
    level: str = "V",
) -> tuple[int, int]:
    """
    Auto-detect the number of sources and destinations on the router.

    Uses binary search over .RD{n} (destination mnemonic) and .RT{n}
    (source mnemonic) commands.  The router responds with:
      - .RD{n},{name}  →  valid (n is in range)
      - .E             →  out of range

    Returns (max_sources, max_destinations), falling back to defaults
    on any error.
    """
    _LOGGER.debug("Probing router size at %s:%d", host, port)

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port),
            timeout=10,
        )
    except (asyncio.TimeoutError, OSError) as err:
        _LOGGER.warning("Could not connect for size probing: %s", err)
        return DEFAULT_MAX_SOURCES, DEFAULT_MAX_DESTINATIONS

    async def send_cmd(cmd: str) -> str | None:
        """Send one command and return the first non-empty response line."""
        try:
            writer.write(cmd.encode())
            await writer.drain()
            # Drain any earlier unsolicited messages (connect burst), then
            # read up to 3 lines looking for the direct reply.
            for _ in range(3):
                raw = await asyncio.wait_for(reader.readline(), timeout=_PROBE_TIMEOUT)
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                # Skip unsolicited route updates — they're not our response
                if line.startswith(".UV") or line.startswith(".P"):
                    continue
                return line
        except (asyncio.TimeoutError, OSError):
            pass
        return None

    def is_valid_response(response: str | None, prefix: str) -> bool:
        """Return True if response looks like a valid mnemonic reply."""
        if response is None:
            return False
        if response.startswith(".E"):
            return False
        # Accept either .RD{n},name  or  .RT{n},name
        return response.upper().startswith(prefix.upper())

    async def binary_search_max(cmd_prefix: str) -> int:
        """
        Binary search for the highest valid index.

        cmd_prefix is '.RD' for destinations or '.RT' for sources.
        Response prefix to validate is the same (e.g. '.RD').
        """
        lo, hi, result = 1, _PROBE_MAX, 0
        while lo <= hi:
            mid = (lo + hi) // 2
            cmd = f"{cmd_prefix}{mid}\r"
            resp = await send_cmd(cmd)
            _LOGGER.debug("Probe %s → %r", cmd.strip(), resp)
            if is_valid_response(resp, cmd_prefix):
                result = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return result if result > 0 else DEFAULT_MAX_SOURCES

    try:
        max_dsts = await asyncio.wait_for(
            binary_search_max(".RD"), timeout=_DETECT_TIMEOUT
        )
        max_srcs = await asyncio.wait_for(
            binary_search_max(".RT"), timeout=_DETECT_TIMEOUT
        )
    except asyncio.TimeoutError:
        _LOGGER.warning("Router size detection timed out — using defaults")
        max_srcs, max_dsts = DEFAULT_MAX_SOURCES, DEFAULT_MAX_DESTINATIONS
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass

    _LOGGER.info(
        "Detected router: %d sources, %d destinations", max_srcs, max_dsts
    )
    return max_srcs, max_dsts


class EvertzQuartzConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle the config flow for Evertz Quartz."""

    VERSION = 1

    def __init__(self) -> None:
        self._host: str = ""
        self._port: int = DEFAULT_PORT
        self._detected_sources: int = DEFAULT_MAX_SOURCES
        self._detected_destinations: int = DEFAULT_MAX_DESTINATIONS
        self._detection_attempted: bool = False

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        """Step 1: get host + port, validate connection, probe size."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = user_input[CONF_HOST]
            port = user_input[CONF_PORT]

            try:
                await _validate_connection(host, port)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            else:
                self._host = host
                self._port = port
                # Move to size detection step
                return await self.async_step_detect()

        schema = vol.Schema(
            {
                vol.Required(CONF_HOST): str,
                vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_detect(self, user_input: dict | None = None) -> FlowResult:
        """
        Step 2: auto-detect sources/destinations, show pre-filled confirm form.

        If user_input is None we're arriving here for the first time —
        run the probe then show the form.  When the user submits the form
        we create the config entry.
        """
        errors: dict[str, str] = {}

        if user_input is None:
            # Run detection (may take a few seconds)
            srcs, dsts = await _probe_router_size(self._host, self._port)
            self._detected_sources = srcs
            self._detected_destinations = dsts

        if user_input is not None:
            # User confirmed / overrode the detected values — create entry
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

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_MAX_SOURCES,
                    default=self._detected_sources,
                ): vol.All(int, vol.Range(min=1, max=_PROBE_MAX)),
                vol.Required(
                    CONF_MAX_DESTINATIONS,
                    default=self._detected_destinations,
                ): vol.All(int, vol.Range(min=1, max=_PROBE_MAX)),
                vol.Required(CONF_LEVELS, default=DEFAULT_LEVELS): str,
            }
        )
        return self.async_show_form(
            step_id="detect",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                "host": self._host,
                "port": str(self._port),
                "detected_sources": str(self._detected_sources),
                "detected_destinations": str(self._detected_destinations),
            },
        )

    @staticmethod
    def async_get_options_flow(config_entry):
        """Return the options flow handler."""
        from .options_flow import EvertzQuartzOptionsFlow
        return EvertzQuartzOptionsFlow(config_entry)


class CannotConnect(Exception):
    """Error to indicate we cannot connect."""
