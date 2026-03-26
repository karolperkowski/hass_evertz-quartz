"""Evertz Quartz protocol TCP client."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable

from .const import (
    QUARTZ_ACK,
    QUARTZ_CONNECT_TIMEOUT,
    QUARTZ_RECONNECT_DELAY,
)

_LOGGER = logging.getLogger(__name__)

# Unsolicited route update pattern: .UV[levels][dest],[src]
# e.g.  .UVV003,001   or   .UVVABC003,001
RE_ROUTE_UPDATE = re.compile(
    r"^\.UV([A-Za-z]*)(\d+),(\d+)$"
)

# Mnemonic response patterns
RE_DEST_MNEMONIC = re.compile(r"^\.RD(\d+),(.+)$")
RE_SRC_MNEMONIC = re.compile(r"^\.RT(\d+),(.+)$")


class QuartzClient:
    """Asyncio TCP client for the Evertz Quartz remote control protocol."""

    def __init__(
        self,
        host: str,
        port: int,
        max_sources: int,
        max_destinations: int,
        levels: str,
        route_callback: Callable[[int, int, str], None] | None = None,
        connection_callback: Callable[[bool], None] | None = None,
    ) -> None:
        self.host = host
        self.port = port
        self.max_sources = max_sources
        self.max_destinations = max_destinations
        self.levels = levels

        # State: dest_number -> source_number (1-based)
        self.routes: dict[int, int] = {}
        # Mnemonics: number -> label
        self.source_names: dict[int, str] = {}
        self.destination_names: dict[int, str] = {}

        self._route_callback = route_callback
        self._connection_callback = connection_callback

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._listen_task: asyncio.Task | None = None
        self._running = False
        self._connected = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the client loop (connect + reconnect)."""
        self._running = True
        self._listen_task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        """Stop the client and close the connection."""
        self._running = False
        if self._listen_task:
            self._listen_task.cancel()
        await self._disconnect()

    async def route(self, destination: int, source: int, levels: str | None = None) -> bool:
        """
        Send a route command to the router.

        Command format:  .SV[levels][dest_padded],[src_padded]\r
        e.g.             .SVV003,001\r
        """
        if not self._connected or self._writer is None:
            _LOGGER.warning("Cannot route: not connected to router")
            return False

        lvl = levels or self.levels
        dest_str = str(destination).zfill(3)
        src_str = str(source).zfill(3)
        cmd = f".SV{lvl}{dest_str},{src_str}\r"

        try:
            _LOGGER.debug("Sending route command: %s", cmd.strip())
            self._writer.write(cmd.encode())
            await self._writer.drain()
            return True
        except (OSError, ConnectionResetError) as err:
            _LOGGER.error("Error sending route command: %s", err)
            await self._disconnect()
            return False

    async def query_all_routes(self) -> None:
        """
        Poll the current route for every destination.
        Command: .QL[levels][dest]\r  — query a specific destination's route.
        """
        if not self._connected or self._writer is None:
            return
        for dest in range(1, self.max_destinations + 1):
            dest_str = str(dest).zfill(3)
            cmd = f".QL{self.levels}{dest_str}\r"
            try:
                self._writer.write(cmd.encode())
                await self._writer.drain()
                await asyncio.sleep(0.02)  # small gap to avoid flooding
            except OSError:
                break

    async def query_all_mnemonics(self) -> None:
        """
        Read source and destination mnemonic names from the router.
        Commands:
          .RD{dest}\r  — read destination mnemonic
          .RT{src}\r   — read source mnemonic
        """
        if not self._connected or self._writer is None:
            return
        try:
            for dest in range(1, self.max_destinations + 1):
                self._writer.write(f".RD{dest}\r".encode())
                await self._writer.drain()
                await asyncio.sleep(0.02)
            for src in range(1, self.max_sources + 1):
                self._writer.write(f".RT{src}\r".encode())
                await self._writer.drain()
                await asyncio.sleep(0.02)
        except OSError as err:
            _LOGGER.warning("Error querying mnemonics: %s", err)

    # ------------------------------------------------------------------
    # Internal connection management
    # ------------------------------------------------------------------

    async def _run_loop(self) -> None:
        """Persistent connect-and-listen loop with reconnection."""
        while self._running:
            try:
                await self._connect()
                # On fresh connection, poll current state + mnemonics
                await self.query_all_mnemonics()
                await asyncio.sleep(0.5)
                await self.query_all_routes()
                await self._listen()
            except asyncio.CancelledError:
                break
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Connection error: %s — reconnecting in %ss", err, QUARTZ_RECONNECT_DELAY)
            finally:
                await self._disconnect()

            if self._running:
                await asyncio.sleep(QUARTZ_RECONNECT_DELAY)

    async def _connect(self) -> None:
        """Open TCP connection to the router."""
        _LOGGER.debug("Connecting to Evertz Quartz router at %s:%s", self.host, self.port)
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port),
            timeout=QUARTZ_CONNECT_TIMEOUT,
        )
        self._connected = True
        _LOGGER.info("Connected to Evertz Quartz router at %s:%s", self.host, self.port)
        if self._connection_callback:
            self._connection_callback(True)

    async def _disconnect(self) -> None:
        """Close the TCP connection cleanly."""
        self._connected = False
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            self._writer = None
            self._reader = None
        if self._connection_callback:
            self._connection_callback(False)

    async def _listen(self) -> None:
        """Read lines from the router and dispatch to handlers."""
        assert self._reader is not None
        while self._running and self._connected:
            try:
                raw = await asyncio.wait_for(self._reader.readline(), timeout=60)
            except asyncio.TimeoutError:
                # Send a keepalive / heartbeat query
                if self._writer:
                    try:
                        self._writer.write(b".QL\r")
                        await self._writer.drain()
                    except OSError:
                        break
                continue

            if not raw:
                _LOGGER.warning("Router closed the connection")
                break

            line = raw.decode(errors="replace").strip()
            if not line:
                continue

            _LOGGER.debug("Received: %s", line)
            self._dispatch(line)

    def _dispatch(self, line: str) -> None:
        """Parse a single line and update internal state."""
        # --- Unsolicited route update: .UV[levels][dest],[src] ---
        m = RE_ROUTE_UPDATE.match(line)
        if m:
            levels_str = m.group(1)
            dest = int(m.group(2))
            src = int(m.group(3))
            _LOGGER.debug("Route update: dest=%d src=%d levels=%s", dest, src, levels_str)
            self.routes[dest] = src
            if self._route_callback:
                self._route_callback(dest, src, levels_str)
            return

        # --- Destination mnemonic response: .RD{dest},{name} ---
        m = RE_DEST_MNEMONIC.match(line)
        if m:
            num = int(m.group(1))
            name = m.group(2).strip()
            self.destination_names[num] = name
            _LOGGER.debug("Destination %d name: %s", num, name)
            return

        # --- Source mnemonic response: .RT{src},{name} ---
        m = RE_SRC_MNEMONIC.match(line)
        if m:
            num = int(m.group(1))
            name = m.group(2).strip()
            self.source_names[num] = name
            _LOGGER.debug("Source %d name: %s", num, name)
            return

        # --- Acknowledge ---
        if line == QUARTZ_ACK:
            return

        # --- Power-on / reset ---
        if ".P" in line:
            _LOGGER.info("Router power-on/reset detected, re-polling state")
            asyncio.create_task(self.query_all_routes())
            return

        _LOGGER.debug("Unhandled message: %s", line)
