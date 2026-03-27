"""Evertz Quartz protocol TCP client.

IMPORTANT — MAGNUM Order vs Port Number
========================================
MAGNUM communicates entirely in Order numbers (the sequential profile index,
column 'Order' in profile_availability.csv). It does NOT expose Quartz
crosspoint Port Numbers over this interface.

  .UV1,360   means destination Order=1 routed to source Order=360
  .SVV001,360 routes destination Order=1 to source Order=360

Port numbers from the CSV are stored for reference (source_port_map /
destination_port_map) but are NOT used in protocol commands.

All dicts (routes, source_names, destination_names) are keyed by Order.
"""

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
    QUARTZ_ACK,
)

_LOGGER = logging.getLogger(__name__)

# .UV[levels][dest_order],[src_order]  e.g. .UV1,360  or  .UVV001,360
RE_ROUTE_UPDATE = re.compile(r"^\.UV([A-Za-z]*)(\d+),(\d+)$")
# .A[levels][dest_order],[src_order]  — .I interrogate response
RE_ROUTE_REPLY  = re.compile(r"^\.A([A-Za-z]*)(\d+),(\d+)$")
# Mnemonic responses (only used on non-MAGNUM routers)
RE_DEST_MNEMONIC = re.compile(r"^\.RD(\d+),(.+)$")
RE_SRC_MNEMONIC  = re.compile(r"^\.RT(\d+),(.+)$")


@dataclass
class QuartzStats:
    connect_time: float | None = None
    disconnect_time: float | None = None
    reconnect_count: int = 0
    messages_received: int = 0
    messages_sent: int = 0
    route_updates: int = 0
    errors: list = field(default_factory=list)

    def record_error(self, msg: str) -> None:
        self.errors.append(f"{time.strftime('%H:%M:%S')} {msg}")
        self.errors = self.errors[-20:]


class QuartzClient:
    """Asyncio TCP client for the Evertz Quartz / MAGNUM remote control protocol."""

    def __init__(
        self,
        host: str,
        port: int,
        max_sources: int,
        max_destinations: int,
        levels: str,
        csv_loaded: bool = False,
        route_callback: Callable[[int, int, str], None] | None = None,
        mnemonic_callback: Callable[[], None] | None = None,
        connection_callback: Callable[[bool], None] | None = None,
        reconnect_delay: int = DEFAULT_RECONNECT_DELAY,
        connect_timeout: int = DEFAULT_CONNECT_TIMEOUT,
    ) -> None:
        self.host = host
        self.port = port
        self.max_sources = max_sources
        self.max_destinations = max_destinations
        self.levels = levels
        self.csv_loaded = csv_loaded
        self.reconnect_delay = reconnect_delay
        self.connect_timeout = connect_timeout

        # All keyed by Order (MAGNUM's numbering)
        self.routes: dict[int, int] = {}           # dest_order → src_order
        self.source_names: dict[int, str] = {}     # src_order → name
        self.destination_names: dict[int, str] = {}# dst_order → name

        # Port maps stored for reference / diagnostics only — not used in commands
        self.src_port_map: dict[int, int] = {}     # order → quartz_port (diagnostics only)
        self.dst_port_map: dict[int, int] = {}     # order → quartz_port (diagnostics only)

        self.stats = QuartzStats()
        self._route_callback = route_callback
        self._mnemonic_callback = mnemonic_callback
        self._connection_callback = connection_callback
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._listen_task: asyncio.Task | None = None
        self._running = False
        self._connected = False
        self._mnemonics_expected = 0
        self._mnemonics_received = 0

    # ── Public API ────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        self._listen_task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._running = False
        if self._listen_task:
            self._listen_task.cancel()
        await self._disconnect()

    def update_options(
        self,
        reconnect_delay: int | None = None,
        connect_timeout: int | None = None,
    ) -> None:
        if reconnect_delay is not None:
            self.reconnect_delay = reconnect_delay
        if connect_timeout is not None:
            self.connect_timeout = connect_timeout

    async def route(self, destination: int, source: int, levels: str | None = None) -> bool:
        """
        Route source Order to destination Order.

        Sends: .SV[levels][dest_order_padded],[src_order_padded]
        Example for dest Order=1, src Order=360:  .SVV001,360
        Immediately updates optimistic state — MAGNUM may also send .UV confirmation.
        """
        if not self._connected or self._writer is None:
            msg = "Cannot route: not connected"
            _LOGGER.warning(msg)
            self.stats.record_error(msg)
            return False

        lvl = levels or self.levels
        cmd = f".SV{lvl}{str(destination).zfill(3)},{str(source).zfill(3)}\r"
        try:
            _LOGGER.debug("TX → %s", cmd.strip())
            self._writer.write(cmd.encode())
            await self._writer.drain()
            self.stats.messages_sent += 1

            # Optimistic update — reflect change immediately in HA
            prev = self.routes.get(destination)
            self.routes[destination] = source
            if prev != source:
                _LOGGER.debug(
                    "Optimistic route: dest Order=%d → src Order=%d (was %s)",
                    destination, source, prev,
                )
                if self._route_callback:
                    self._route_callback(destination, source, lvl)
            return True
        except (OSError, ConnectionResetError) as err:
            msg = f"Route command failed: {err}"
            _LOGGER.error(msg)
            self.stats.record_error(msg)
            await self._disconnect()
            return False

    async def query_all_routes(self) -> None:
        """
        Send .I{level}{dest} (Interrogate Route) for each destination Order.
        Response format: .A{level}{dest},{src}(cr)
        MAGNUM may not respond — optimistic state is used as fallback.
        """
        if not self._connected or self._writer is None:
            return

        dst_orders = list(range(1, self.max_destinations + 1))
        _LOGGER.debug("Interrogating route state for destination order(s): %s", dst_orders)
        try:
            for order in dst_orders:
                cmd = f".I{self.levels}{order}\r"
                _LOGGER.debug("TX → %s", cmd.strip())
                self._writer.write(cmd.encode())
                await self._writer.drain()
                self.stats.messages_sent += 1
                await asyncio.sleep(0.05)
        except OSError as err:
            _LOGGER.warning("Error sending .I interrogate: %s", err)

    async def query_all_mnemonics(self) -> None:
        """
        Query .RT / .RD for all Order indices.
        Skipped when csv_loaded=True — CSV names are authoritative.
        Only useful for non-MAGNUM routers that respond to these commands.
        """
        if not self._connected or self._writer is None:
            return
        if self.csv_loaded:
            _LOGGER.debug("Mnemonic query skipped — CSV names loaded")
            return

        total = self.max_destinations + self.max_sources
        self._mnemonics_expected = total
        self._mnemonics_received = 0
        _LOGGER.debug(
            "Querying mnemonics: %d dst + %d src (Order indices)",
            self.max_destinations, self.max_sources,
        )
        try:
            for order in range(1, self.max_destinations + 1):
                cmd = f".RD{order}\r"
                _LOGGER.debug("TX → %s", cmd.strip())
                self._writer.write(cmd.encode())
                await self._writer.drain()
                self.stats.messages_sent += 1
                await asyncio.sleep(0.02)
            for order in range(1, self.max_sources + 1):
                cmd = f".RT{order}\r"
                _LOGGER.debug("TX → %s", cmd.strip())
                self._writer.write(cmd.encode())
                await self._writer.drain()
                self.stats.messages_sent += 1
                await asyncio.sleep(0.02)
        except OSError as err:
            _LOGGER.warning("Error querying mnemonics: %s", err)
            self.stats.record_error(f"Mnemonic query failed: {err}")

    def get_diagnostics(self) -> dict:
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
                "csv_loaded": self.csv_loaded,
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

    # ── Connection management ─────────────────────────────────────────────

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self._connect()
                await self.query_all_routes()
                if not self.csv_loaded:
                    await self.query_all_mnemonics()
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
                await asyncio.sleep(self.reconnect_delay)

    async def _connect(self) -> None:
        _LOGGER.debug("Connecting to %s:%d (timeout %ds)", self.host, self.port, self.connect_timeout)
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
            _LOGGER.info("Disconnected from %s:%d", self.host, self.port)
        if self._connection_callback:
            self._connection_callback(False)

    async def _listen(self) -> None:
        assert self._reader is not None
        _LOGGER.debug("Listening for messages from %s:%d", self.host, self.port)

        while self._running and self._connected:
            try:
                # Quartz protocol terminates messages with \r (0x0D) only — not \n.
                # Using readuntil(b'\r') ensures we receive each message immediately
                # rather than waiting 60 seconds for a \n that never arrives.
                raw = await asyncio.wait_for(
                    self._reader.readuntil(b'\r'), timeout=60
                )
            except asyncio.IncompleteReadError as e:
                # EOF mid-message
                raw = e.partial
                if not raw:
                    _LOGGER.warning("Router closed connection (EOF)")
                    break
            except asyncio.TimeoutError:
                # 60s of silence — connection likely still alive (MAGNUM holds it open)
                # but check by attempting a known-safe query
                if self._writer:
                    try:
                        cmd = f".I{self.levels}1\r"
                        _LOGGER.debug("TX → %s (keepalive probe)", cmd.strip())
                        self._writer.write(cmd.encode())
                        await self._writer.drain()
                        self.stats.messages_sent += 1
                    except OSError:
                        break
                continue

            if not raw:
                continue

            line = raw.decode(errors="replace").strip()
            if not line:
                continue

            _LOGGER.debug("RX ← %r", line)
            self.stats.messages_received += 1
            self._dispatch(line)

    def _dispatch(self, line: str) -> None:
        """Parse incoming message. All numbers are Order indices."""

        # Unsolicited route update: .UV[levels][dest_order],[src_order]
        # e.g. .UV1,360  or  .UVV001,360
        m = RE_ROUTE_UPDATE.match(line)
        if m:
            levels_str  = m.group(1)
            dest_order  = int(m.group(2))
            src_order   = int(m.group(3))
            prev        = self.routes.get(dest_order)
            self.routes[dest_order] = src_order
            self.stats.route_updates += 1
            if prev != src_order:
                dest_name = self.destination_names.get(dest_order, f"Dest {dest_order}")
                src_name  = self.source_names.get(src_order,  f"Src {src_order}")
                _LOGGER.debug(
                    "Route update: %s (Order %d) → %s (Order %d)",
                    dest_name, dest_order, src_name, src_order,
                )
            if self._route_callback:
                self._route_callback(dest_order, src_order, levels_str)
            return

        # .I interrogate response: .A[levels][dest_order],[src_order]
        m = RE_ROUTE_REPLY.match(line)
        if m:
            dest_order = int(m.group(2))
            src_order  = int(m.group(3))
            prev = self.routes.get(dest_order)
            self.routes[dest_order] = src_order
            if prev != src_order:
                _LOGGER.debug(
                    "Route sync (.I reply): dest Order=%d → src Order=%d",
                    dest_order, src_order,
                )
                if self._route_callback:
                    self._route_callback(dest_order, src_order, m.group(1))
            return

        # Destination mnemonic (non-MAGNUM routers): .RD{order},{name}
        m = RE_DEST_MNEMONIC.match(line)
        if m:
            order = int(m.group(1))
            name  = m.group(2).strip()
            self.destination_names[order] = name
            _LOGGER.debug("Destination Order %d → %s", order, name)
            self._on_mnemonic_received()
            return

        # Source mnemonic (non-MAGNUM routers): .RT{order},{name}
        m = RE_SRC_MNEMONIC.match(line)
        if m:
            order = int(m.group(1))
            name  = m.group(2).strip()
            self.source_names[order] = name
            _LOGGER.debug("Source Order %d → %s", order, name)
            self._on_mnemonic_received()
            return

        if line == QUARTZ_ACK:
            _LOGGER.debug("ACK")
            return

        if ".P" in line:
            _LOGGER.info("Router power-on/reset — re-querying routes")
            asyncio.create_task(self.query_all_routes())
            return

        _LOGGER.debug("Unhandled: %r", line)

    def _on_mnemonic_received(self) -> None:
        self._mnemonics_received += 1
        if self._mnemonic_callback:
            self._mnemonic_callback()
        if self._mnemonics_received >= self._mnemonics_expected > 0:
            _LOGGER.debug("All %d mnemonics received", self._mnemonics_expected)
