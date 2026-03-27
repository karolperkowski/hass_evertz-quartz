"""Evertz Quartz protocol TCP client."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .const import (
    DEFAULT_CLIENT_VERBOSE,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_RECONNECT_DELAY,
    DEFAULT_VERBOSE_LOGGING,
    QUARTZ_ACK,
    QUARTZ_ROUTE_SYNC_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)

RE_ROUTE_UPDATE  = re.compile(r"^\.UV([A-Za-z]*)(\d+),(\d+)$")
RE_ROUTE_REPLY   = re.compile(r"^\.A([A-Za-z]*)(\d+),(\d+)$")   # .QL response
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
        csv_loaded: bool = False,
        route_callback: Callable[[int, int, str], None] | None = None,
        mnemonic_callback: Callable[[], None] | None = None,
        connection_callback: Callable[[bool], None] | None = None,
        verbose_logging: bool = DEFAULT_VERBOSE_LOGGING,
        client_verbose: bool = DEFAULT_CLIENT_VERBOSE,
        reconnect_delay: int = DEFAULT_RECONNECT_DELAY,
        connect_timeout: int = DEFAULT_CONNECT_TIMEOUT,
    ) -> None:
        self.host = host
        self.port = port
        self.max_sources = max_sources
        self.max_destinations = max_destinations
        self.levels = levels
        # csv_loaded: skip .RT/.RD queries — names come from CSV stored in entry.data
        self.csv_loaded = csv_loaded
        self.src_port_map: dict[int, int] = src_port_map or {n: n for n in range(1, max_sources + 1)}
        self.dst_port_map: dict[int, int] = dst_port_map or {n: n for n in range(1, max_destinations + 1)}
        self.verbose_logging = verbose_logging
        self.client_verbose = client_verbose
        self.reconnect_delay = reconnect_delay
        self.connect_timeout = connect_timeout

        self.routes: dict[int, int] = {}           # quartz_port → quartz_src_port
        self.source_names: dict[int, str] = {}     # quartz_port → name
        self.destination_names: dict[int, str] = {}
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
        verbose_logging: bool | None = None,
        client_verbose: bool | None = None,
        reconnect_delay: int | None = None,
        connect_timeout: int | None = None,
    ) -> None:
        if verbose_logging is not None:
            self.verbose_logging = verbose_logging
        if client_verbose is not None:
            changed = self.client_verbose != client_verbose
            self.client_verbose = client_verbose
            if changed:
                _LOGGER.info("Client verbose logging %s", "ENABLED" if client_verbose else "disabled")
        if reconnect_delay is not None:
            self.reconnect_delay = reconnect_delay
        if connect_timeout is not None:
            self.connect_timeout = connect_timeout

    async def route(self, destination: int, source: int, levels: str | None = None) -> bool:
        """
        Send .SV command and update optimistic state immediately.
        destination / source are Quartz port numbers.
        """
        if not self._connected or self._writer is None:
            msg = "Cannot route: not connected"
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
            # Optimistic: update local state immediately — MAGNUM never sends .UV
            prev = self.routes.get(destination)
            self.routes[destination] = source
            if prev != source:
                _LOGGER.debug(
                    "Optimistic route: dest port %d → src port %d (was %s)",
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
        Send .QL for every destination port and collect responses for up to
        QUARTZ_ROUTE_SYNC_TIMEOUT seconds. Routers that respond will update
        client.routes; routers that don't (MAGNUM) will leave optimistic state.
        """
        if not self._connected or self._writer is None:
            return

        dst_ports = sorted(self.dst_port_map.values())
        _LOGGER.debug("Querying route state for port(s): %s", dst_ports)

        try:
            for port in dst_ports:
                cmd = f".QL{self.levels}{str(port).zfill(3)}\r"
                self._tx(cmd)
                self._writer.write(cmd.encode())
                await self._writer.drain()
                self.stats.messages_sent += 1
                await asyncio.sleep(0.02)
        except OSError as err:
            _LOGGER.warning("Error sending .QL queries: %s", err)
            return

        # Wait briefly for responses — any .AV replies handled in _dispatch
        _LOGGER.debug("Waiting up to %ds for .QL responses", QUARTZ_ROUTE_SYNC_TIMEOUT)
        await asyncio.sleep(QUARTZ_ROUTE_SYNC_TIMEOUT)

    async def query_all_mnemonics(self) -> None:
        """
        Query .RT / .RD for all ports in the port maps.
        Called only when no CSV is loaded — skipped otherwise.
        """
        if not self._connected or self._writer is None:
            return
        if self.csv_loaded:
            _LOGGER.debug("Mnemonic query skipped — CSV names loaded")
            return

        dst_ports = sorted(self.dst_port_map.values())
        src_ports = sorted(self.src_port_map.values())
        total = len(dst_ports) + len(src_ports)
        self._mnemonics_expected = total
        self._mnemonics_received = 0
        _LOGGER.debug("Querying mnemonics: %d dst + %d src ports", len(dst_ports), len(src_ports))

        try:
            for port in dst_ports:
                self._tx(f".RD{port}\r")
                self._writer.write(f".RD{port}\r".encode())
                await self._writer.drain()
                self.stats.messages_sent += 1
                await asyncio.sleep(0.02)
            for port in src_ports:
                self._tx(f".RT{port}\r")
                self._writer.write(f".RT{port}\r".encode())
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
                "verbose_logging": self.verbose_logging,
                "client_verbose": self.client_verbose,
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

    # ── Internal helpers ─────────────────────────────────────────────────

    def _tx(self, cmd: str) -> None:
        if self.client_verbose:
            _LOGGER.debug("TX → %s", cmd.strip())

    def _rx(self, line: str) -> None:
        if self.client_verbose:
            _LOGGER.debug("RX ← %s", line)

    def _on_mnemonic_received(self) -> None:
        self._mnemonics_received += 1
        if self._mnemonic_callback:
            self._mnemonic_callback()
        if self._mnemonics_received >= self._mnemonics_expected > 0:
            _LOGGER.debug("All %d mnemonics received", self._mnemonics_expected)

    # ── Connection management ─────────────────────────────────────────────

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self._connect()
                # Step 1: query route state (best-effort, includes 2s wait)
                await self.query_all_routes()
                # Step 2: query names only if no CSV loaded
                if not self.csv_loaded:
                    await self.query_all_mnemonics()
                # Step 3: listen for unsolicited messages
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
            _LOGGER.info("Disconnected from Evertz Quartz router at %s:%d", self.host, self.port)
        if self._connection_callback:
            self._connection_callback(False)

    async def _listen(self) -> None:
        assert self._reader is not None
        _LOGGER.debug("Listening for messages from router at %s:%d", self.host, self.port)

        while self._running and self._connected:
            try:
                raw = await asyncio.wait_for(self._reader.readline(), timeout=60)
            except asyncio.TimeoutError:
                # Keepalive ping
                if self._writer:
                    try:
                        self._tx(".I\r")
                        self._writer.write(b".I\r")
                        await self._writer.drain()
                        self.stats.messages_sent += 1
                    except OSError:
                        break
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
                _LOGGER.debug("Route update: dest port %d → src port %d", dest, src)
            if self._route_callback:
                self._route_callback(dest, src, levels_str)
            return

        # .QL response: .A[levels][dest],[src]
        m = RE_ROUTE_REPLY.match(line)
        if m:
            dest = int(m.group(2))
            src  = int(m.group(3))
            prev = self.routes.get(dest)
            self.routes[dest] = src
            if prev != src:
                _LOGGER.debug("Route sync (.QL reply): dest port %d → src port %d", dest, src)
                if self._route_callback:
                    self._route_callback(dest, src, m.group(1))
            return

        # Destination mnemonic: .RD{port},{name}
        m = RE_DEST_MNEMONIC.match(line)
        if m:
            port = int(m.group(1))
            name = m.group(2).strip()
            self.destination_names[port] = name
            _LOGGER.debug("Destination port %d → %s", port, name)
            self._on_mnemonic_received()
            return

        # Source mnemonic: .RT{port},{name}
        m = RE_SRC_MNEMONIC.match(line)
        if m:
            port = int(m.group(1))
            name = m.group(2).strip()
            self.source_names[port] = name
            _LOGGER.debug("Source port %d → %s", port, name)
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
