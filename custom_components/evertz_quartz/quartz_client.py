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
import contextlib
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

# Abort a mnemonic sweep (.RD or .RT) after this many consecutive .E replies —
# MAGNUM-family controllers reject every mnemonic query, and without the guard
# a 1164-source profile produces a thousand-message error storm on connect.
MNEMONIC_ABORT_AFTER = 5

# An .E arriving within this many seconds of an .SV is treated as the router
# rejecting that take — the optimistic route update is rolled back.
SV_ERROR_WINDOW = 5.0

# Reconnect backoff cap. The configured reconnect_delay is the floor; repeated
# failures double the delay up to this ceiling, reset on a successful connect.
MAX_RECONNECT_DELAY = 120

# Connect-time sweeps write this many commands per drain() with a 50 ms pause
# between batches — large matrices sync in seconds instead of minutes while
# still pacing the controller.
SWEEP_BATCH = 8
SWEEP_BATCH_PAUSE = 0.05

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
    interrogate_rejected: int = 0           # .E replies attributed to .I (expected on over-provisioned profiles)
    mnemonic_rejected: int = 0              # .E replies attributed to .RD/.RT (controller rejects mnemonic queries)
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
        sync_callback: Callable[[], None] | None = None,
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

        # Orders marked Hidden? in the CSV profile. Kept in the name/port maps
        # (MAGNUM still uses their Orders in .UV/.SV) but excluded from the
        # source dropdown options.
        self.hidden_sources: set[int] = set()
        self.hidden_destinations: set[int] = set()

        self.stats = QuartzStats()
        self._route_callback = route_callback
        self._mnemonic_callback = mnemonic_callback
        self._connection_callback = connection_callback
        self._notify_callback = notify_callback
        self._lock_callback = lock_callback
        # Called after the connect-time sync sweep (routes/locks/names) has
        # been fully sent — lets HA dismiss the "synchronizing" notification.
        self._sync_callback = sync_callback
        # Tracks (kind, order) pairs already warned — prevents log/notification spam
        self._warned_orders: set[tuple[str, int]] = set()
        # Highest Order numbers actually seen from the router (in .UV/.A traffic).
        # Lower bound on the live profile size — drives detection + warnings.
        self.max_src_order_seen = 0
        self.max_dst_order_seen = 0
        # Lock state: dest_order → lock_value (0=unlocked, 255=locked, other=partial)
        self.locks: dict[int, int] = {}
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._listen_task: asyncio.Task | None = None
        # Background re-query tasks spawned by the .P handler — kept so
        # stop() can cancel them and they aren't garbage-collected mid-run.
        self._bg_tasks: set[asyncio.Task] = set()
        self._running = False
        self._connected = False
        self._mnemonics_expected = 0
        self._mnemonics_received = 0
        # .E attribution for mnemonic sweeps: queries still awaiting a reply
        # and the current consecutive-.E streak (drives the sweep abort).
        self._mnemonic_pending = 0
        self._mnemonic_consec_e = 0
        # .I interrogations awaiting .A/.E — sweep/button queries only, NOT
        # keepalive probes (a controller that ignores .I would otherwise grow
        # this without bound and swallow every future .E).
        self._interrogate_pending = 0
        # Last optimistic take awaiting confirmation: (monotonic_ts, dest, prev_src)
        self._pending_sv: tuple[float, int, int | None] | None = None
        # Exponential reconnect backoff state (0 = next delay is the floor)
        self._current_backoff = 0

    # ── Public API ────────────────────────────────────────────────────────

    @property
    def connected(self) -> bool:
        """True while the TCP connection to the router is established."""
        return self._connected

    async def start(self) -> None:
        self._running = True
        self._listen_task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._running = False
        for task in list(self._bg_tasks):
            task.cancel()
        self._bg_tasks.clear()
        if self._listen_task:
            self._listen_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._listen_task
            self._listen_task = None
        await self._disconnect()

    def _spawn_bg_task(self, coro) -> None:
        """Run a background coroutine with a tracked, stop()-cancellable task."""
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

    def update_options(
        self,
        reconnect_delay: int | None = None,
        connect_timeout: int | None = None,
    ) -> None:
        if reconnect_delay is not None:
            self.reconnect_delay = reconnect_delay
            self._current_backoff = 0  # apply the new floor immediately
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

            # Optimistic update — reflect change immediately in HA.
            # Remember the previous source so an .E reply can roll it back.
            prev = self.routes.get(destination)
            self._pending_sv = (time.monotonic(), destination, prev)
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

        self._log.debug(
            "%sInterrogating route state for destinations 1-%d", self._pfx, self.max_destinations
        )
        try:
            for start in range(1, self.max_destinations + 1, SWEEP_BATCH):
                buf = bytearray()
                for order in range(start, min(start + SWEEP_BATCH, self.max_destinations + 1)):
                    cmd = f".I{self.levels}{order}\r"
                    self._log.debug("%sTX → %s", self._pfx, cmd.strip())
                    self.stats.record_trace("TX", cmd.strip())
                    # Pending before write — the reader may reply during drain()
                    self._interrogate_pending += 1
                    self.stats.messages_sent += 1
                    self.stats.interrogate_sent += 1
                    buf += cmd.encode()
                self._writer.write(bytes(buf))
                await self._writer.drain()
                await asyncio.sleep(SWEEP_BATCH_PAUSE)
        except OSError as err:
            self._log.warning("%sError sending .I interrogate: %s", self._pfx, err)

    async def wait_interrogation_drain(self, timeout: float = 10.0, stall: float = 2.0) -> None:
        """Wait until every outstanding .I interrogation was answered.

        Returns early when no reply progress is made for ``stall`` seconds
        (controllers that ignore .I never answer) or after ``timeout``.
        """
        deadline = time.monotonic() + timeout
        last_progress = time.monotonic()
        last_count = self.stats.interrogate_replied + self.stats.interrogate_rejected
        while time.monotonic() < deadline:
            if self._interrogate_pending <= 0:
                return
            count = self.stats.interrogate_replied + self.stats.interrogate_rejected
            if count != last_count:
                last_count = count
                last_progress = time.monotonic()
            elif time.monotonic() - last_progress >= stall:
                return
            await asyncio.sleep(0.1)

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
        self._mnemonic_pending = 0
        self._log.debug(
            "%sQuerying mnemonics: %d dst + %d src (Order indices)", self._pfx,
            self.max_destinations, self.max_sources,
        )
        try:
            await self._mnemonic_sweep(".RD", self.max_destinations)
            await self._mnemonic_sweep(".RT", self.max_sources)
        except OSError as err:
            self._log.warning("%sError querying mnemonics: %s", self._pfx, err)
            self.stats.record_error(f"Mnemonic query failed: {err}")

    async def _mnemonic_sweep(self, query: str, count: int) -> None:
        """Send one mnemonic query type for Orders 1..count.

        Aborts after MNEMONIC_ABORT_AFTER consecutive .E replies — MAGNUM-family
        controllers reject mnemonic queries, and continuing would flood the
        connection with errors. (The reader task runs concurrently, so replies
        are processed while the sweep is still sending.)
        """
        self._mnemonic_consec_e = 0
        for start in range(1, count + 1, SWEEP_BATCH):
            if self._mnemonic_consec_e >= MNEMONIC_ABORT_AFTER:
                self._log.info(
                    "%sAborting %s mnemonic sweep at Order %d — %d consecutive .E "
                    "replies (controller rejects %s queries)",
                    self._pfx, query, start, self._mnemonic_consec_e, query,
                )
                return
            buf = bytearray()
            for order in range(start, min(start + SWEEP_BATCH, count + 1)):
                cmd = f"{query}{order}\r"
                self._log.debug("%sTX → %s", self._pfx, cmd.strip())
                self.stats.record_trace("TX", cmd.strip())
                # Count as pending before writing — the reader task may
                # process replies during drain()/sleep() below.
                self._mnemonic_pending += 1
                self.stats.messages_sent += 1
                buf += cmd.encode()
            self._writer.write(bytes(buf))
            await self._writer.drain()
            await asyncio.sleep(SWEEP_BATCH_PAUSE)

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
                "hidden_sources": len(self.hidden_sources),
                "hidden_destinations": len(self.hidden_destinations),
            },
            "detection": {
                "max_source_order_seen":      self.max_src_order_seen,
                "max_destination_order_seen": self.max_dst_order_seen,
                "suggested_max_sources":      max(self.max_sources, self.max_src_order_seen),
                "suggested_max_destinations": max(self.max_destinations, self.max_dst_order_seen),
            },
            "stats": {
                "messages_sent":        self.stats.messages_sent,
                "messages_received":    self.stats.messages_received,
                "route_updates_uv":     self.stats.route_updates,
                "sv_sent":              self.stats.sv_sent,
                "interrogate_sent":     self.stats.interrogate_sent,
                "interrogate_replied":  self.stats.interrogate_replied,
                "interrogate_rejected": self.stats.interrogate_rejected,
                "mnemonic_rejected":    self.stats.mnemonic_rejected,
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

    def estimated_sync_seconds(self) -> int:
        """Rough duration of the connect-time sync sweep, derived from the
        batched send pacing (SWEEP_BATCH commands per SWEEP_BATCH_PAUSE for
        .I routes, .BI locks, and .RD/.RT names when no CSV is loaded) plus
        a reply grace period."""
        def batches(n: int) -> int:
            return -(-n // SWEEP_BATCH)  # ceil division

        secs = batches(self.max_destinations) * SWEEP_BATCH_PAUSE * 2  # .I + .BI
        if not self.csv_loaded:
            secs += batches(self.max_destinations + self.max_sources) * SWEEP_BATCH_PAUSE
        return max(3, round(secs + 2))

    async def _run_loop(self) -> None:
        while self._running:
            reader_task: asyncio.Task | None = None
            try:
                await self._connect()
                # Start processing replies before the connect-time sweep so
                # incoming .E storms can abort the mnemonic sweep early and
                # .A/.BA replies apply as soon as they arrive.
                reader_task = asyncio.create_task(self._listen())
                await self.query_all_routes()
                await self._query_all_locks()
                if not self.csv_loaded:
                    await self.query_all_mnemonics()
                if self._sync_callback:
                    self._sync_callback()
                await reader_task
                reader_task = None
            except asyncio.CancelledError:
                break
            except Exception as err:  # noqa: BLE001
                msg = f"Connection error: {err}"
                self._log.error("%s%s — will reconnect", self._pfx, msg)
                self.stats.record_error(msg)
            finally:
                if reader_task is not None:
                    reader_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await reader_task
                await self._disconnect()

            if self._running:
                delay = self._next_reconnect_delay()
                self._log.debug("%sReconnecting in %ds", self._pfx, delay)
                await asyncio.sleep(delay)

    def _next_reconnect_delay(self) -> int:
        """Exponential backoff: floor at the configured reconnect_delay,
        double per consecutive failed cycle, capped at MAX_RECONNECT_DELAY.
        Reset by a successful connect."""
        if self._current_backoff:
            self._current_backoff = min(self._current_backoff * 2, MAX_RECONNECT_DELAY)
        else:
            self._current_backoff = max(1, self.reconnect_delay)
        return self._current_backoff

    async def _connect(self) -> None:
        self._log.debug("%sConnecting to %s:%d (timeout %ds)", self._pfx, self.host, self.port, self.connect_timeout)
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port),
            timeout=self.connect_timeout,
        )
        self._connected = True
        self.stats.connect_time = time.time()
        self.stats.reconnect_count += 1
        # Fresh connection: reset backoff and stale reply accounting
        self._current_backoff = 0
        self._interrogate_pending = 0
        self._mnemonic_pending = 0
        self._mnemonic_consec_e = 0
        self._pending_sv = None
        self._log.info(
            "%sConnected to Evertz Quartz router at %s:%d (connection #%d)",
            self._pfx, self.host, self.port, self.stats.reconnect_count,
        )
        if self._connection_callback:
            self._connection_callback(True)

    async def _query_all_locks(self) -> None:
        """Interrogate lock state for all configured destinations on connect."""
        if not self._connected or self._writer is None:
            return
        try:
            for start in range(1, self.max_destinations + 1, SWEEP_BATCH):
                buf = bytearray()
                for order in range(start, min(start + SWEEP_BATCH, self.max_destinations + 1)):
                    cmd = f".BI{order}\r"
                    self._log.debug("%sTX → %s (lock interrogate)", self._pfx, cmd.strip())
                    self.stats.record_trace("TX", cmd.strip())
                    self.stats.messages_sent += 1
                    buf += cmd.encode()
                self._writer.write(bytes(buf))
                await self._writer.drain()
                await asyncio.sleep(SWEEP_BATCH_PAUSE)
        except OSError as err:
            self._log.warning("%sLock interrogation sweep failed: %s", self._pfx, err)

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
            # A .UV for the optimistically-routed destination confirms the take
            if self._pending_sv and self._pending_sv[1] == dest_order:
                self._pending_sv = None
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
            self._interrogate_pending = max(0, self._interrogate_pending - 1)
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
            # Interrogate replies also reveal Orders the router uses
            self._check_order_range("src", src_order)
            self._check_order_range("dst", dest_order)
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
            self._spawn_bg_task(self.query_all_routes())
            self._spawn_bg_task(self._query_all_locks())
            return

        # .E = error response from router
        if line == ".E":
            self._handle_error_reply()
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

    def _handle_error_reply(self) -> None:
        """Attribute an incoming .E to the most likely outstanding command.

        .E carries no context, so attribution is best-effort: a take awaiting
        confirmation first (the router rejected it — roll back the optimistic
        route update), then outstanding .I interrogations (an .E per
        nonexistent destination is *expected* on over-provisioned profiles —
        it drives detection), then outstanding mnemonic queries (MAGNUM
        rejects .RD/.RT). Sweep rejections are counted in dedicated stats
        instead of recent_errors so real errors stay visible.
        """
        if self._pending_sv is not None:
            ts, dest, prev = self._pending_sv
            self._pending_sv = None
            if time.monotonic() - ts <= SV_ERROR_WINDOW:
                if prev is None:
                    self.routes.pop(dest, None)
                else:
                    self.routes[dest] = prev
                dest_name = self.destination_names.get(dest, f"Dest {dest}")
                msg = (
                    f"Router rejected take on {dest_name} (Order {dest}) — "
                    f"rolled back to previous source ({prev})"
                )
                self._log.warning("%s%s", self._pfx, msg)
                self.stats.record_error(msg)
                if self._route_callback:
                    self._route_callback(dest, prev if prev is not None else 0, self.levels)
                return
        if self._interrogate_pending > 0:
            self._interrogate_pending -= 1
            self.stats.interrogate_rejected += 1
            self._log.debug(
                "%s.E attributed to .I interrogate (destination does not exist)", self._pfx
            )
            return
        if self._mnemonic_pending > 0:
            self._mnemonic_pending -= 1
            self._mnemonic_consec_e += 1
            self.stats.mnemonic_rejected += 1
            self._log.debug(
                "%s.E attributed to mnemonic query (%d rejected so far)",
                self._pfx, self.stats.mnemonic_rejected,
            )
            return
        self._log.warning(
            "%sRouter returned .E (error) — last command was rejected (bad level, "
            "dest out of range, or malformed command)", self._pfx
        )
        self.stats.record_error("Router returned .E")

    def _check_order_range(self, kind: str, order: int) -> None:
        """Record the highest Order seen, and warn once if it exceeds the max."""
        if kind == "src":
            self.max_src_order_seen = max(self.max_src_order_seen, order)
        else:
            self.max_dst_order_seen = max(self.max_dst_order_seen, order)
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
        self._mnemonic_pending = max(0, self._mnemonic_pending - 1)
        self._mnemonic_consec_e = 0
        if self._mnemonic_callback:
            self._mnemonic_callback()
        if self._mnemonics_received >= self._mnemonics_expected > 0:
            self._log.debug("%sAll %d mnemonics received", self._pfx, self._mnemonics_expected)
