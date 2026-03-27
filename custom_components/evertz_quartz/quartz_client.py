"""Evertz Quartz protocol TCP client."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .const import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_RECONNECT_DELAY,
    DEFAULT_VERBOSE_LOGGING,
    QUARTZ_ACK,
    QUARTZ_POLL_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

# Unsolicited route update:  .UV[levels][dest],[src]   e.g.  .UVV003,001
RE_ROUTE_UPDATE = re.compile(r"^\.UV([A-Za-z]*)(\d+),(\d+)$")

# Mnemonic responses
RE_DEST_MNEMONIC = re.compile(r"^\.RD(\d+),(.+)$")
RE_SRC_MNEMONIC  = re.compile(r"^\.RT(\d+),(.+)$")


@dataclass
class QuartzStats:
    """Runtime counters exposed via diagnostics."""

    connect_time: float | None = None
    disconnect_time: float | None = None
    reconnect_count: int = 0
    messages_received: int = 0
    messages_sent: int = 0
    route_updates: int = 0
    errors: list = field(default_factory=list)  # last 20

    def record_error(self, msg: str) -> None:
        self.errors.append(f"{time.strftime('%H:%M:%S')} {msg}")
        self.errors = self.errors[-20:]


class QuartzClient:
    """Asyncio TCP client for the Evertz Quartz remote control protocol."""

    def __init__(
        self,
        host: str,
        port: int,
        max_sources: int,
        max_destinations: int,
        levels: str,
        src_port_map: dict[int, int] | None = None,
        dst_port_map: dict[int, int] | None = None,
        route_callback: Callable[[int, int, str], None] | None = None,
        mnemonic_callback: Callable[[], None] | None = None,
        connection_callback: Callable[[bool], None] | None = None,
        verbose_logging: bool = DEFAULT_VERBOSE_LOGGING,
        reconnect_delay: int = DEFAULT_RECONNECT_DELAY,
        connect_timeout: int = DEFAULT_CONNECT_TIMEOUT,
    ) -> None:
        self.host = host
        self.port = port
        self.max_sources = max_sources
        self.max_destinations = max_destinations
        self.levels = levels
        # Port maps: {order_index: quartz_port_number}
        # Identity mapping when not provided (contiguous routers)
        self.src_port_map: dict[int, int] = src_port_map or {n: n for n in range(1, max_sources + 1)}
        self.dst_port_map: dict[int, int] = dst_port_map or {n: n for n in range(1, max_destinations + 1)}
        self.verbose_logging = verbose_logging
        self.reconnect_delay = reconnect_delay
        self.connect_timeout = connect_timeout

        # Live state
        self.routes: dict[int, int] = {}
        self.source_names: dict[int, str] = {}
        self.destination_names: dict[int, str] = {}
        self.stats = QuartzStats()

        # Callbacks
        self._route_callback = route_callback
        self._mnemonic_callback = mnemonic_callback   # fired after each mnemonic update
        self._connection_callback = connection_callback

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._listen_task: asyncio.Task | None = None
        self._running = False
        self._connected = False

        # Track how many mnemonic responses we expect so we can fire
        # the callback once all names have arrived.
        self._mnemonics_expected: int = 0
        self._mnemonics_received: int = 0

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

    def update_options(
        self,
        verbose_logging: bool | None = None,
        reconnect_delay: int | None = None,
        connect_timeout: int | None = None,
    ) -> None:
        """Apply updated options at runtime — no reconnect needed."""
        if verbose_logging is not None:
            changed = self.verbose_logging != verbose_logging
            self.verbose_logging = verbose_logging
            if changed:
                _LOGGER.info(
                    "Verbose TCP logging %s",
                    "ENABLED" if verbose_logging else "disabled",
                )
        if reconnect_delay is not None:
            self.reconnect_delay = reconnect_delay
            _LOGGER.debug("Reconnect delay updated to %ds", reconnect_delay)
        if connect_timeout is not None:
            self.connect_timeout = connect_timeout
            _LOGGER.debug("Connect timeout updated to %ds", connect_timeout)

    async def route(self, destination: int, source: int, levels: str | None = None) -> bool:
        """Route a source to a destination.  .SV[levels][dest_3],[src_3]\\r"""
        if not self._connected or self._writer is None:
            msg = "Cannot route: not connected to router"
            _LOGGER.warning(msg)
            self.stats.record_error(msg)
            return False

        lvl = levels or self.levels
        cmd = f".SV{lvl}{str(destination).zfill(3)},{str(source).zfill(3)}\r"
        try:
            self._tx(cmd)
            self._writer.write(cmd.encode())
            await self._writer.drain()
            self.stats.messages_sent += 1
            return True
        except (OSError, ConnectionResetError) as err:
            msg = f"Error sending route command: {err}"
            _LOGGER.error(msg)
            self.stats.record_error(msg)
            await self._disconnect()
            return False

    async def query_all_routes(self) -> None:
        """Poll current route for every destination using actual Quartz port numbers."""
        if not self._connected or self._writer is None:
            return
        dst_ports = sorted(self.dst_port_map.values())
        _LOGGER.debug("Polling %d destination route(s): ports %s", len(dst_ports), dst_ports)
        for port in dst_ports:
            cmd = f".QL{self.levels}{str(port).zfill(3)}\r"
            try:
                self._tx(cmd)
                self._writer.write(cmd.encode())
                await self._writer.drain()
                self.stats.messages_sent += 1
                await asyncio.sleep(0.02)
            except OSError:
                break

    async def query_all_mnemonics(self) -> None:
        """Fetch source & destination labels from the router.

        Queries every Quartz port number in the port maps.
        Router responses always override any previously held names,
        keeping HA in sync if labels change on the router.
        """
        if not self._connected or self._writer is None:
            return

        dst_ports = sorted(self.dst_port_map.values())
        src_ports = sorted(self.src_port_map.values())
        total = len(dst_ports) + len(src_ports)

        self._mnemonics_expected = total
        self._mnemonics_received = 0
        _LOGGER.debug(
            "Fetching mnemonics: %d destination(s), %d source(s)",
            len(dst_ports), len(src_ports),
        )

        try:
            for port in dst_ports:
                cmd = f".RD{port}\r"
                self._tx(cmd)
                self._writer.write(cmd.encode())
                await self._writer.drain()
                self.stats.messages_sent += 1
                await asyncio.sleep(0.02)
            for port in src_ports:
                cmd = f".RT{port}\r"
                self._tx(cmd)
                self._writer.write(cmd.encode())
                await self._writer.drain()
                self.stats.messages_sent += 1
                await asyncio.sleep(0.02)
        except OSError as err:
            _LOGGER.warning("Error querying mnemonics: %s", err)
            self.stats.record_error(f"Mnemonic query failed: {err}")

    def get_diagnostics(self) -> dict:
        """Return a runtime snapshot for HA diagnostics."""
        return {
            "connection": {
                "host": self.host,
                "port": self.port,
                "connected": self._connected,
                "connect_time": self.stats.connect_time,
                "disconnect_time": self.stats.disconnect_time,
                "reconnect_count": self.stats.reconnect_count,
            },
            "options": {
                "max_sources": self.max_sources,
                "max_destinations": self.max_destinations,
                "levels": self.levels,
                "verbose_logging": self.verbose_logging,
                "reconnect_delay": self.reconnect_delay,
                "connect_timeout": self.connect_timeout,
            },
            "stats": {
                "messages_sent": self.stats.messages_sent,
                "messages_received": self.stats.messages_received,
                "route_updates": self.stats.route_updates,
                "recent_errors": self.stats.errors,
            },
            "routes": {str(k): v for k, v in sorted(self.routes.items())},
            "source_names": {str(k): v for k, v in sorted(self.source_names.items())},
            "destination_names": {str(k): v for k, v in sorted(self.destination_names.items())},
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _tx(self, cmd: str) -> None:
        if self.verbose_logging:
            _LOGGER.debug("TX → %s", cmd.strip())

    def _rx(self, line: str) -> None:
        if self.verbose_logging:
            _LOGGER.debug("RX ← %s", line)

    def _on_mnemonic_received(self) -> None:
        """Increment counter and fire the mnemonic callback when all names are in."""
        self._mnemonics_received += 1
        # Fire immediately on every update so the UI shows names as they trickle in.
        # The callback is cheap (just schedules async_write_ha_state).
        if self._mnemonic_callback:
            self._mnemonic_callback()
        if self._mnemonics_received == self._mnemonics_expected:
            _LOGGER.debug(
                "All %d mnemonics received — %d sources, %d destinations named",
                self._mnemonics_expected,
                len(self.source_names),
                len(self.destination_names),
            )

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self._connect()
                await self.query_all_mnemonics()
                await asyncio.sleep(0.5)
                await self.query_all_routes()
                await self._listen()
            except asyncio.CancelledError:
                break
            except Exception as err:  # noqa: BLE001
                msg = f"Connection error: {err}"
                _LOGGER.error("%s — reconnecting in %ds", msg, self.reconnect_delay)
                self.stats.record_error(msg)
            finally:
                await self._disconnect()

            if self._running:
                _LOGGER.debug("Waiting %ds before reconnect", self.reconnect_delay)
                await asyncio.sleep(self.reconnect_delay)

    async def _connect(self) -> None:
        _LOGGER.debug(
            "Connecting to %s:%d (timeout %ds)", self.host, self.port, self.connect_timeout
        )
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port),
            timeout=self.connect_timeout,
        )
        self._connected = True
        self.stats.connect_time = time.time()
        self.stats.reconnect_count += 1
        _LOGGER.info(
            "Connected to Evertz Quartz router at %s:%d (connection #%d)",
            self.host, self.port, self.stats.reconnect_count,
        )
        if self._connection_callback:
            self._connection_callback(True)

    async def _disconnect(self) -> None:
        was_connected = self._connected
        self._connected = False
        self.stats.disconnect_time = time.time()
        if self._writer:
            try:
                self._writer.close()
                await self._writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            self._writer = None
            self._reader = None
        if was_connected:
            _LOGGER.info(
                "Disconnected from Evertz Quartz router at %s:%d", self.host, self.port
            )
        if self._connection_callback:
            self._connection_callback(False)

    async def _listen(self) -> None:
        assert self._reader is not None
        _LOGGER.debug("Listening for messages from router at %s:%d", self.host, self.port)
        last_poll = time.time()

        while self._running and self._connected:
            try:
                raw = await asyncio.wait_for(self._reader.readline(), timeout=60)
            except asyncio.TimeoutError:
                if self._writer:
                    try:
                        self._tx(".QL\r")
                        self._writer.write(b".QL\r")
                        await self._writer.drain()
                        self.stats.messages_sent += 1
                    except OSError:
                        break
                if time.time() - last_poll > QUARTZ_POLL_INTERVAL:
                    _LOGGER.debug("Periodic route re-poll triggered")
                    await self.query_all_routes()
                    last_poll = time.time()
                continue

            if not raw:
                _LOGGER.warning("Router closed the connection (EOF)")
                break

            line = raw.decode(errors="replace").strip()
            if not line:
                continue

            self._rx(line)
            self.stats.messages_received += 1
            self._dispatch(line)

    def _dispatch(self, line: str) -> None:
        """Parse a single message and update internal state."""

        # Unsolicited route update: .UV[levels][dest],[src]
        m = RE_ROUTE_UPDATE.match(line)
        if m:
            levels_str = m.group(1)
            dest = int(m.group(2))
            src  = int(m.group(3))
            prev = self.routes.get(dest)
            self.routes[dest] = src
            self.stats.route_updates += 1
            if prev != src:
                _LOGGER.debug(
                    "Route change: dest=%d  %s → %d  (levels=%s)",
                    dest, str(prev) if prev else "?", src, levels_str or self.levels,
                )
            if self._route_callback:
                self._route_callback(dest, src, levels_str)
            return

        # Destination mnemonic: .RD{dest},{name}
        m = RE_DEST_MNEMONIC.match(line)
        if m:
            num  = int(m.group(1))
            name = m.group(2).strip()
            self.destination_names[num] = name
            _LOGGER.debug("Destination label  %3d → %s", num, name)
            self._on_mnemonic_received()
            return

        # Source mnemonic: .RT{src},{name}
        m = RE_SRC_MNEMONIC.match(line)
        if m:
            num  = int(m.group(1))
            name = m.group(2).strip()
            self.source_names[num] = name
            _LOGGER.debug("Source label       %3d → %s", num, name)
            self._on_mnemonic_received()
            return

        # Acknowledge
        if line == QUARTZ_ACK:
            _LOGGER.debug("ACK received")
            return

        # Power-on / reset
        if ".P" in line:
            _LOGGER.info("Router power-on/reset — re-polling routes and mnemonics")
            asyncio.create_task(self.query_all_mnemonics())
            asyncio.create_task(self.query_all_routes())
            return

        _LOGGER.debug("Unhandled message: %r", line)
