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
# .BA{dest},{value}  — lock state update (255=locked, 0=unlocked)
RE_LOCK_UPDATE  = re.compile(r"^\.BA(\d+),(\d+)$")
# Mnemonic responses (only used on non-MAGNUM routers)
# Mnemonic responses (non-MAGNUM routers):
# 8-char:  .RAD{dest},{name}  or  .RAS{src},{name}  (comma-separated)
#          .RAD{name}         or  .RAS{name}          (no number - some firmware)
# 10-char: .RAE{dest},{name}  or  .RAT{src},{name}
# Some old firmware echoes the command: .RD{n},{name} or .RS{n},{name}
RE_DEST_MNEMONIC = re.compile(r"^(?:\.RA[DE](\d+),(.+)|\.R[AD](\d+),(.+))$")
RE_SRC_MNEMONIC  = re.compile(r"^(?:\.RA[TS](\d+),(.+)|\.R[ST](\d+),(.+))$")


@dataclass
class QuartzStats:
    connect_time: float | None = None
    disconnect_time: float | None = None
    last_rx_time: float | None = None       # timestamp of most recent received message
    last_uv_time: float | None = None       # timestamp of most recent .UV update
    last_sv_time: float | None = None       # timestamp of most recent .SV sent
    reconnect_count: int = 0
    messages_received: int = 0
    messages_sent: int = 0
    route_updates: int = 0                  # .UV messages received
    interrogate_sent: int = 0               # .I commands sent
    interrogate_replied: int = 0            # .A replies received
    sv_sent: int = 0                        # .SV commands sent
    unhandled: int = 0                      # messages not matched by any parser
    errors: list = field(default_factory=list)
    # Protocol trace: last 100 TX/RX lines with timestamps — for diagnostics
    trace: list = field(default_factory=list)

    def record_error(self, msg: str) -> None:
        self.errors.append(f"{time.strftime('%H:%M:%S')} {msg}")
        self.errors = self.errors[-20:]

    def record_trace(self, direction: str, line: str) -> None:
        """direction: 'TX' or 'RX'"""
        entry = f"{time.strftime('%H:%M:%S.') + f'{int(time.time() * 1000) % 1000:03d}'} {direction} {line}"
        self.trace.append(entry)
        self.trace = self.trace[-100:]


class QuartzClient:
    """Asyncio TCP client for the Evertz Quartz / MAGNUM remote control protocol."""

    def __init__(
        self,
        host: str,
        port: int,
        max_sources: int,
        max_destinations: int,
        levels: str,
        router_name: str = "",
        csv_loaded: bool = False,
        route_callback: Callable[[int, int, str], None] | None = None,
        mnemonic_callback: Callable[[], None] | None = None,
        connection_callback: Callable[[bool], None] | None = None,
        notify_callback: Callable[[str, int], None] | None = None,
        lock_callback: Callable[[int, int], None] | None = None,
        reconnect_delay: int = DEFAULT_RECONNECT_DELAY,
        connect_timeout: int = DEFAULT_CONNECT_TIMEOUT,
    ) -> None:
        self.host = host
        self.port = port
        self.max_sources = max_sources
        self.max_destinations = max_destinations
        self.levels = levels
        self.router_name = router_name
        self.csv_loaded = csv_loaded
        self.reconnect_delay = reconnect_delay
        self.connect_timeout = connect_timeout
        # Named logger per router — filterable in HA logs, targeted by log level entity
        # e.g. custom_components.evertz_quartz.quartz_client.MY-ROUTER
        self._log = logging.getLogger(
            f"{__name__}.{router_name}" if router_name else __name__
        )
        # Prefix injected into every message for at-a-glance identification
        self._pfx = f"[{router_name}] " if router_name else ""

        # All keyed by Order (MAGNUM's numbering)
        self.routes: dict[int, int] = {}            # dest_order → src_order
        self.source_names: dict[int, str] = {}      # src_order → name
        self.destination_names: dict[int, str] = {} # dst_order → name

        # Namespace (Device Short Name) — keyed by Order.
        # Empty when no CSV loaded or CSV has no Short Name column.
        # Used to block cross-namespace routing.
        self.source_namespaces: dict[int, str] = {}      # src_order → short_name
        self.destination_namespaces: dict[int, str] = {} # dst_order → short_name

        # Port maps stored for reference / diagnostics only — not used in commands
        self.src_port_map: dict[int, int] = {}     # order → quartz_port (diagnostics only)
        self.dst_port_map: dict[int, int] = {}     # order → quartz_port (diagnostics only)

        self.stats = QuartzStats()
        self._route_callback = route_callback
        self._mnemonic_callback = mnemonic_callback
        self._connection_callback = connection_callback
        self._notify_callback = notify_callback
        self._lock_callback = lock_callback
        # Tracks (kind, order) pairs already warned — prevents log/notification spam
        self._warned_orders: set[tuple[str, int]] = set()
        # Lock state: dest_order → lock_value (0=unlocked, 255=locked, other=partial)
        self.locks: dict[int, int] = {}
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
            self._log.warning(msg)
            self.stats.record_error(msg)
            return False

        lvl = levels or self.levels
        cmd = f".SV{lvl}{str(destination).zfill(3)},{str(source).zfill(3)}\r"
        try:
            cmd_stripped = cmd.strip()
            self._log.debug("%sTX → %s", self._pfx, cmd_stripped)
            self.stats.record_trace("TX", cmd_stripped)
            self._writer.write(cmd.encode())
            await self._writer.drain()
            self.stats.messages_sent += 1
            self.stats.sv_sent += 1
            self.stats.last_sv_time = time.time()

            # Optimistic update — reflect change immediately in HA
            prev = self.routes.get(destination)
            self.routes[destination] = source
            if prev != source:
                self._log.debug(
                    "%sOptimistic route: dest Order=%d → src Order=%d (was %s)", self._pfx,
                    destination, source, prev,
                )
                if self._route_callback:
                    self._route_callback(destination, source, lvl)
            return True
        except (OSError, ConnectionResetError) as err:
            msg = f"Route command failed: {err}"
            self._log.error(msg)
            self.stats.record_error(msg)
            await self._disconnect()
            return False

    async def lock_destination(self, destination: int) -> bool:
        """
        Send .BL{dest} to lock a destination.
        Per AN65: no explicit response — .BA only if state changes.
        Always follows with .BI to confirm actual state.
        """
        ok = await self._send_lock_cmd(f".BL{destination}\r", destination, "lock")
        if ok:
            await asyncio.sleep(0.1)
            await self.query_lock_state(destination)
        return ok

    async def unlock_destination(self, destination: int) -> bool:
        """
        Send .BU{dest} to unlock a destination.
        Per AN65: no explicit response — .BA only if state changes.
        Always follows with .BI to confirm actual state.
        Note: lock values 1-254 are panel locks (Q-link) — .BU may not clear them.
        """
        ok = await self._send_lock_cmd(f".BU{destination}\r", destination, "unlock")
        if ok:
            await asyncio.sleep(0.1)
            await self.query_lock_state(destination)
        return ok

    async def query_lock_state(self, destination: int) -> None:
        """Send .BI{dest} to interrogate current lock state."""
        if not self._connected or self._writer is None:
            return
        cmd = f".BI{destination}\r"
        try:
            self._log.debug("%sTX → %s (lock interrogate)", self._pfx, cmd.strip())
            self.stats.record_trace("TX", cmd.strip())
            self._writer.write(cmd.encode())
            await self._writer.drain()
            self.stats.messages_sent += 1
        except OSError as err:
            self._log.warning("%sLock interrogate failed: %s", self._pfx, err)

    async def _send_lock_cmd(self, cmd: str, destination: int, action: str) -> bool:
        if not self._connected or self._writer is None:
            msg = f"Cannot {action} dest {destination}: not connected"
            self._log.warning(msg)
            self.stats.record_error(msg)
            return False
        try:
            cmd_stripped = cmd.strip()
            self._log.debug("%sTX → %s", self._pfx, cmd_stripped)
            self.stats.record_trace("TX", cmd_stripped)
            self._writer.write(cmd.encode())
            await self._writer.drain()
            self.stats.messages_sent += 1
            return True
        except (OSError, ConnectionResetError) as err:
            msg = f"Lock command failed: {err}"
            self._log.error(msg)
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
        self._log.debug("%sInterrogating route state for destination order(s): %s", self._pfx, dst_orders)
        try:
            for order in dst_orders:
                cmd = f".I{self.levels}{order}\r"
                cmd_stripped = cmd.strip()
                self._log.debug("%sTX → %s", self._pfx, cmd_stripped)
                self.stats.record_trace("TX", cmd_stripped)
                self._writer.write(cmd.encode())
                await self._writer.drain()
                self.stats.messages_sent += 1
                self.stats.interrogate_sent += 1
                await asyncio.sleep(0.05)
        except OSError as err:
            self._log.warning("%sError sending .I interrogate: %s", self._pfx, err)

    async def query_all_mnemonics(self) -> None:
        """
        Query source/destination mnemonics for all Order indices.
        Uses .RT (10-char source name) and .RD (8-char destination name).
        Per AN65:  .RS = 8-char source, .RT = 10-char source
                   .RD = 8-char destination, .RE = 10-char destination
        We use .RT / .RD as these cover the most modern router firmware.
        Responses: .RAT{n},{name} / .RAD{n},{name} (or legacy .RT/{RD} echo)
        Skipped when csv_loaded=True — CSV names are authoritative.
        Only useful for non-MAGNUM routers that respond to these commands.
        """
        if not self._connected or self._writer is None:
            return
        if self.csv_loaded:
            self._log.debug("%sMnemonic query skipped — CSV names loaded", self._pfx)
            return

        total = self.max_destinations + self.max_sources
        self._mnemonics_expected = total
        self._mnemonics_received = 0
        self._log.debug(
            "%sQuerying mnemonics: %d dst + %d src (Order indices)", self._pfx,
            self.max_destinations, self.max_sources,
        )
        try:
            for order in range(1, self.max_destinations + 1):
                cmd = f".RD{order}\r"
                cmd_stripped = cmd.strip()
                self._log.debug("%sTX → %s", self._pfx, cmd_stripped)
                self.stats.record_trace("TX", cmd_stripped)
                self._writer.write(cmd.encode())
                await self._writer.drain()
                self.stats.messages_sent += 1
                await asyncio.sleep(0.02)
            for order in range(1, self.max_sources + 1):
                cmd = f".RT{order}\r"
                cmd_stripped = cmd.strip()
                self._log.debug("%sTX → %s", self._pfx, cmd_stripped)
                self.stats.record_trace("TX", cmd_stripped)
                self._writer.write(cmd.encode())
                await self._writer.drain()
                self.stats.messages_sent += 1
                await asyncio.sleep(0.02)
        except OSError as err:
            self._log.warning("%sError querying mnemonics: %s", self._pfx, err)
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
                "messages_sent":        self.stats.messages_sent,
                "messages_received":    self.stats.messages_received,
                "route_updates_uv":     self.stats.route_updates,
                "sv_sent":              self.stats.sv_sent,
                "interrogate_sent":     self.stats.interrogate_sent,
                "interrogate_replied":  self.stats.interrogate_replied,
                "unhandled_messages":   self.stats.unhandled,
                "last_rx_time":         self.stats.last_rx_time,
                "last_uv_time":         self.stats.last_uv_time,
                "last_sv_time":         self.stats.last_sv_time,
                "recent_errors":        self.stats.errors,
            },
            "protocol_trace": self.stats.trace,
            "routes": {str(k): v for k, v in sorted(self.routes.items())},
            "locks":  {str(k): v for k, v in sorted(self.locks.items())},
            "source_names": {str(k): v for k, v in sorted(self.source_names.items())},
            "destination_names": {str(k): v for k, v in sorted(self.destination_names.items())},
            "source_namespaces": {str(k): v for k, v in sorted(self.source_namespaces.items())},
            "destination_namespaces": {str(k): v for k, v in sorted(self.destination_namespaces.items())},
        }

    # ── Connection management ─────────────────────────────────────────────

    async def _run_loop(self) -> None:
        while self._running:
            try:
                await self._connect()
                await self.query_all_routes()
                await self._query_all_locks()
                if not self.csv_loaded:
                    await self.query_all_mnemonics()
                await self._listen()
            except asyncio.CancelledError:
                break
            except Exception as err:  # noqa: BLE001
                msg = f"Connection error: {err}"
                self._log.error("%s%s — reconnecting in %ds", self._pfx, msg, self.reconnect_delay)
                self.stats.record_error(msg)
            finally:
                await self._disconnect()

            if self._running:
                await asyncio.sleep(self.reconnect_delay)

    async def _connect(self) -> None:
        self._log.debug("%sConnecting to %s:%d (timeout %ds)", self._pfx, self.host, self.port, self.connect_timeout)
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port),
            timeout=self.connect_timeout,
        )
        self._connected = True
        self.stats.connect_time = time.time()
        self.stats.reconnect_count += 1
        self._log.info(
            "%sConnected to Evertz Quartz router at %s:%d (connection #%d)",
            self._pfx, self.host, self.port, self.stats.reconnect_count,
        )
        if self._connection_callback:
            self._connection_callback(True)

    async def _query_all_locks(self) -> None:
        """Interrogate lock state for all configured destinations on connect."""
        for order in range(1, self.max_destinations + 1):
            await self.query_lock_state(order)
            await asyncio.sleep(0.05)

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
            self._log.info("%sDisconnected from %s:%d", self._pfx, self.host, self.port)
        if self._connection_callback:
            self._connection_callback(False)

    async def _listen(self) -> None:
        assert self._reader is not None
        self._log.debug("%sListening for messages from %s:%d", self._pfx, self.host, self.port)

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
                    self._log.warning("%sRouter closed connection (EOF)", self._pfx)
                    break
            except asyncio.TimeoutError:
                # 60s of silence — connection likely still alive (MAGNUM holds it open)
                idle = time.time() - (self.stats.last_rx_time or self.stats.connect_time or time.time())
                self._log.debug("%s60s idle (%.0fs since last RX) — sending keepalive probe", self._pfx, idle)
                # but check by attempting a known-safe query
                if self._writer:
                    try:
                        cmd = f".I{self.levels}1\r"
                        cmd_stripped = cmd.strip()
                        self._log.debug("%sTX → %s (keepalive probe, %ds silence)", self._pfx, cmd_stripped, 60)
                        self.stats.record_trace("TX", f"{cmd_stripped} [keepalive]")
                        self._writer.write(cmd.encode())
                        await self._writer.drain()
                        self.stats.messages_sent += 1
                        self.stats.interrogate_sent += 1
                    except OSError:
                        break
                continue

            if not raw:
                continue

            line = raw.decode(errors="replace").strip()
            if not line:
                continue

            self._log.debug("%sRX ← %r", self._pfx, line)
            self.stats.record_trace("RX", line)
            self.stats.messages_received += 1
            self.stats.last_rx_time = time.time()
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
            dest_name = self.destination_names.get(dest_order, f"Dest {dest_order}")
            src_name  = self.source_names.get(src_order,  f"Src {src_order}")
            self.stats.last_uv_time = time.time()
            if prev != src_order:
                self._log.debug(
                    "%sRoute update (.UV): %s (Order %d) → %s (Order %d)", self._pfx,
                    dest_name, dest_order, src_name, src_order,
                )
            else:
                self._log.debug(
                    "%sRoute update (.UV) no change: %s still → %s (Order %d)", self._pfx,
                    dest_name, src_name, src_order,
                )
            if self._route_callback:
                self._route_callback(dest_order, src_order, levels_str)
            # Out-of-range detection — fires once per unique Order per session
            self._check_order_range("src", src_order)
            self._check_order_range("dst", dest_order)
            return

        # .I interrogate response: .A[levels][dest_order],[src_order]
        m = RE_ROUTE_REPLY.match(line)
        if m:
            dest_order = int(m.group(2))
            src_order  = int(m.group(3))
            prev = self.routes.get(dest_order)
            self.routes[dest_order] = src_order
            self.stats.interrogate_replied += 1
            dest_name = self.destination_names.get(dest_order, f"Dest {dest_order}")
            src_name  = self.source_names.get(src_order, f"Src {src_order}")
            if prev != src_order:
                self._log.debug(
                    "%sRoute sync (.I reply): %s (Order %d) → %s (Order %d) [was %s]", self._pfx,
                    dest_name, dest_order, src_name, src_order, prev,
                )
                if self._route_callback:
                    self._route_callback(dest_order, src_order, m.group(1))
            else:
                self._log.debug(
                    "%sRoute confirmed (.I reply): %s → %s (Order %d, no change)", self._pfx,
                    dest_name, src_name, src_order,
                )
            return

        # Destination mnemonic (non-MAGNUM routers): .RD{order},{name}
        m = RE_DEST_MNEMONIC.match(line)
        if m:
            # Groups vary depending on which pattern matched
            order_str = m.group(1) or m.group(3)
            name_str  = m.group(2) or m.group(4)
            if order_str and name_str:
                order = int(order_str)
                name  = name_str.strip()
                self.destination_names[order] = name
                self._log.debug("%sDestination Order %d → %s", self._pfx, order, name)
                self._on_mnemonic_received()
            return

        # Source mnemonic (non-MAGNUM routers): .RT{order},{name}
        m = RE_SRC_MNEMONIC.match(line)
        if m:
            order_str = m.group(1) or m.group(3)
            name_str  = m.group(2) or m.group(4)
            if order_str and name_str:
                order = int(order_str)
                name  = name_str.strip()
                self.source_names[order] = name
                self._log.debug("%sSource Order %d → %s", self._pfx, order, name)
                self._on_mnemonic_received()
            return

        if line == QUARTZ_ACK:
            self._log.debug("%sACK (.A) received", self._pfx)
            return

        # Lock state update: .BA{dest},{value}
        m = RE_LOCK_UPDATE.match(line)
        if m:
            dest_order = int(m.group(1))
            lock_value = int(m.group(2))
            prev       = self.locks.get(dest_order)
            self.locks[dest_order] = lock_value
            dest_name  = self.destination_names.get(dest_order, f"Dest {dest_order}")
            locked     = lock_value > 0
            if prev != lock_value:
                self._log.info(
                    "%sLock state: %s (Order %d) → %s (value=%d)",
                    self._pfx, dest_name, dest_order,
                    "LOCKED" if locked else "UNLOCKED", lock_value,
                )
            else:
                self._log.debug(
                    "%sLock confirmed: %s still %s (value=%d)",
                    self._pfx, dest_name,
                    "LOCKED" if locked else "UNLOCKED", lock_value,
                )
            if self._lock_callback:
                self._lock_callback(dest_order, lock_value)
            return

        if line == ".P":
            self._log.info("%sRouter power-on/reset — re-querying routes and lock state", self._pfx)
            asyncio.create_task(self.query_all_routes())
            asyncio.create_task(self._query_all_locks())
            return

        # .E = error response from router
        if line == ".E":
            self._log.warning(
                "%sRouter returned .E (error) — last command was rejected (bad level, "
                "dest out of range, or malformed command)", self._pfx
            )
            self.stats.record_error("Router returned .E")
            return

        # .XU / .XA / .XE = MAGNUM extension messages (locks, protects, etc.)
        # We don't enable extensions so these come from other connected clients.
        # Parse lock-related ones; log others at DEBUG to avoid inflating unhandled count.
        if line.startswith(".X"):
            self._handle_magnum_extension(line)
            return

        self.stats.unhandled += 1
        raw_hex = line.encode().hex()
        self._log.debug("%sUnhandled: %r (hex: %s)", self._pfx, line, raw_hex)

    def _check_order_range(self, kind: str, order: int) -> None:
        """Warn once per session if an Order number exceeds the configured maximum."""
        limit = self.max_sources if kind == "src" else self.max_destinations
        label = "source" if kind == "src" else "destination"
        conf  = "max_sources" if kind == "src" else "max_destinations"
        key   = (kind, order)
        if order > limit and key not in self._warned_orders:
            self._warned_orders.add(key)
            self._log.warning(
                "%s%s Order %d exceeds configured %s=%d — router profile may have expanded. "
                "Update via Settings → Devices & Services → Evertz Quartz → Configure → Update Profile.",
                self._pfx, label.capitalize(), order, conf, limit,
            )
            if self._notify_callback:
                self._notify_callback(kind, order)

    def _handle_magnum_extension(self, line: str) -> None:
        """
        Handle MAGNUM .X extension messages (section 10.2 of AN65).
        We don't enable extensions (.X,QCX,...) so these arrive from other
        clients on the bus. Parse lock/protect updates; ignore others.

        Formats:
          .XU,ELK,LEVELS,DESTNUM,IDENT,NAME  — lock enabled (unsolicited)
          .XU,DLK,LEVELS,DESTNUM,IDENT,NAME  — lock disabled (unsolicited)
          .XA,ELK,...                          — response to ILK query
          .XE,CMD,...,MESSAGE                  — error
        """
        parts = line.split(",")
        if len(parts) < 3:
            self._log.debug("%sMAGNUM extension (ignored): %r", self._pfx, line)
            return

        cmd_type = parts[0]  # .XU / .XA / .XE
        cmd      = parts[1]  # ELK / DLK / EPT / DPT / QCX / etc.

        if cmd in ("ELK", "EPT") and len(parts) >= 4:
            # Lock/protect enabled for a destination
            try:
                dest_order = int(parts[3])
                prev = self.locks.get(dest_order, 0)
                self.locks[dest_order] = 255  # treat extended lock as unprotected lock
                dest_name = self.destination_names.get(dest_order, f"Dest {dest_order}")
                if prev == 0:
                    self._log.info(
                        "%sLock state (MAGNUM ext %s): %s (Order %d) → LOCKED",
                        self._pfx, cmd, dest_name, dest_order,
                    )
                if self._lock_callback:
                    self._lock_callback(dest_order, 255)
            except (IndexError, ValueError):
                pass
            return

        if cmd in ("DLK", "DPT") and len(parts) >= 4:
            # Lock/protect disabled for a destination
            try:
                dest_order = int(parts[3])
                prev = self.locks.get(dest_order, 0)
                self.locks[dest_order] = 0
                dest_name = self.destination_names.get(dest_order, f"Dest {dest_order}")
                if prev != 0:
                    self._log.info(
                        "%sLock state (MAGNUM ext %s): %s (Order %d) → UNLOCKED",
                        self._pfx, cmd, dest_name, dest_order,
                    )
                if self._lock_callback:
                    self._lock_callback(dest_order, 0)
            except (IndexError, ValueError):
                pass
            return

        if cmd_type == ".XE":
            self._log.warning("%sMAGNUM extension error: %r", self._pfx, line)
            return

        self._log.debug("%sMAGNUM extension (unhandled cmd %s): %r", self._pfx, cmd, line)

    def _on_mnemonic_received(self) -> None:
        self._mnemonics_received += 1
        if self._mnemonic_callback:
            self._mnemonic_callback()
        if self._mnemonics_received >= self._mnemonics_expected > 0:
            self._log.debug("%sAll %d mnemonics received", self._pfx, self._mnemonics_expected)
