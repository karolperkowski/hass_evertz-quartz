"""Tests for QuartzClient._dispatch and the connect-time sweep behavior.

These are pure asyncio tests — no Home Assistant instance needed.
"""

from __future__ import annotations

import asyncio

import pytest

from custom_components.evertz_quartz.quartz_client import (
    MNEMONIC_ABORT_AFTER,
    QuartzClient,
)


class FakeWriter:
    """Minimal StreamWriter stand-in recording every command written."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    def write(self, data: bytes) -> None:
        self.commands.append(data.decode())

    async def drain(self) -> None:
        return

    def close(self) -> None:
        return

    async def wait_closed(self) -> None:
        return


def make_client(**kwargs) -> QuartzClient:
    defaults = dict(
        host="router.local",
        port=6666,
        max_sources=10,
        max_destinations=5,
        levels="V",
        router_name="MY-ROUTER",
    )
    defaults.update(kwargs)
    return QuartzClient(**defaults)


# ── .UV route updates ──────────────────────────────────────────────────────


def test_uv_updates_route_and_fires_callback() -> None:
    calls: list[tuple[int, int, str]] = []
    client = make_client(route_callback=lambda d, s, lv: calls.append((d, s, lv)))
    client._dispatch(".UV1,360")
    assert client.routes == {1: 360}
    assert client.stats.route_updates == 1
    assert calls == [(1, 360, "")]


def test_uv_with_levels_and_padding() -> None:
    client = make_client()
    client._dispatch(".UVV001,042")
    assert client.routes == {1: 42}


def test_uv_no_change_still_counts() -> None:
    calls: list = []
    client = make_client(route_callback=lambda *a: calls.append(a))
    client._dispatch(".UV1,360")
    client._dispatch(".UV1,360")
    assert client.stats.route_updates == 2
    # Callback fires on every .UV (entities decide what to redraw)
    assert len(calls) == 2


def test_uv_tracks_max_orders_seen() -> None:
    client = make_client()
    client._dispatch(".UV4,9")
    assert client.max_dst_order_seen == 4
    assert client.max_src_order_seen == 9


def test_uv_out_of_range_notifies_once_per_order() -> None:
    notified: list[tuple[str, int]] = []
    client = make_client(notify_callback=lambda k, o: notified.append((k, o)))
    client._dispatch(".UV1,11")   # source 11 > max_sources 10
    client._dispatch(".UV1,11")   # repeat — no second notification
    client._dispatch(".UV6,1")    # dest 6 > max_destinations 5
    assert notified == [("src", 11), ("dst", 6)]


# ── .A interrogate replies ─────────────────────────────────────────────────


def test_interrogate_reply_updates_route() -> None:
    calls: list = []
    client = make_client(route_callback=lambda *a: calls.append(a))
    client._dispatch(".AV2,7")
    assert client.routes == {2: 7}
    assert client.stats.interrogate_replied == 1
    assert calls == [(2, 7, "V")]


def test_interrogate_reply_no_change_skips_callback() -> None:
    calls: list = []
    client = make_client(route_callback=lambda *a: calls.append(a))
    client.routes[2] = 7
    client._dispatch(".AV2,7")
    assert calls == []
    assert client.stats.interrogate_replied == 1


def test_bare_ack() -> None:
    client = make_client()
    client._dispatch(".A")
    assert client.stats.unhandled == 0


# ── .BA lock updates ───────────────────────────────────────────────────────


def test_lock_update() -> None:
    locks: list[tuple[int, int]] = []
    client = make_client(lock_callback=lambda d, v: locks.append((d, v)))
    client._dispatch(".BA3,255")
    assert client.locks == {3: 255}
    client._dispatch(".BA3,0")
    assert client.locks == {3: 0}
    assert locks == [(3, 255), (3, 0)]


def test_magnum_extension_lock() -> None:
    locks: list[tuple[int, int]] = []
    client = make_client(lock_callback=lambda d, v: locks.append((d, v)))
    client._dispatch(".XU,ELK,V,2,ident,name")
    assert client.locks[2] == 255
    client._dispatch(".XU,DLK,V,2,ident,name")
    assert client.locks[2] == 0
    assert locks == [(2, 255), (2, 0)]


# ── Mnemonic replies ───────────────────────────────────────────────────────


def test_mnemonic_replies() -> None:
    fired: list[bool] = []
    client = make_client(mnemonic_callback=lambda: fired.append(True))
    client._dispatch(".RAD1,DEST-A")
    client._dispatch(".RAT2,SRC-002")
    # Legacy echo formats
    client._dispatch(".RD3,DEST-C")
    client._dispatch(".RT4,SRC-004")
    assert client.destination_names == {1: "DEST-A", 3: "DEST-C"}
    assert client.source_names == {2: "SRC-002", 4: "SRC-004"}
    assert len(fired) == 4


def test_mnemonic_reply_resets_consecutive_e_streak() -> None:
    client = make_client()
    client._mnemonic_pending = 5
    client._dispatch(".E")
    client._dispatch(".E")
    assert client._mnemonic_consec_e == 2
    client._dispatch(".RAT1,SRC-001")
    assert client._mnemonic_consec_e == 0


# ── .E attribution ─────────────────────────────────────────────────────────


def test_e_attributed_to_interrogate_first() -> None:
    client = make_client()
    client._interrogate_pending = 3
    client._dispatch(".E")
    assert client.stats.interrogate_rejected == 1
    assert client.stats.mnemonic_rejected == 0
    assert client.stats.errors == []


def test_e_attributed_to_mnemonics_when_no_interrogate_outstanding() -> None:
    client = make_client()
    client._mnemonic_pending = 2
    client._dispatch(".E")
    client._dispatch(".E")
    assert client.stats.mnemonic_rejected == 2
    assert client._mnemonic_pending == 0
    assert client._mnemonic_consec_e == 2
    assert client.stats.errors == []


def test_unattributed_e_is_a_real_error() -> None:
    client = make_client()
    client._dispatch(".E")
    assert client.stats.errors != []


# ── Optimistic rollback on .E ──────────────────────────────────────────────


async def test_e_rolls_back_optimistic_route() -> None:
    calls: list = []
    client = make_client(route_callback=lambda *a: calls.append(a))
    client._writer = FakeWriter()
    client._connected = True
    client.routes[1] = 10

    await client.route(1, 360)
    assert client.routes[1] == 360

    client._dispatch(".E")
    assert client.routes[1] == 10
    # Optimistic update then rollback both fired the callback
    assert calls == [(1, 360, "V"), (1, 10, "V")]
    assert any("rolled back" in e for e in client.stats.errors)


async def test_e_rollback_removes_route_when_no_previous_source() -> None:
    client = make_client()
    client._writer = FakeWriter()
    client._connected = True

    await client.route(1, 360)
    client._dispatch(".E")
    assert 1 not in client.routes


async def test_uv_confirmation_disarms_rollback() -> None:
    client = make_client()
    client._writer = FakeWriter()
    client._connected = True
    client.routes[1] = 10

    await client.route(1, 360)
    client._dispatch(".UV1,360")   # router confirms the take
    client._dispatch(".E")         # later .E must not roll back
    assert client.routes[1] == 360


async def test_stale_pending_sv_is_not_rolled_back(monkeypatch) -> None:
    client = make_client()
    client._writer = FakeWriter()
    client._connected = True
    await client.route(1, 360)
    # Age the pending take beyond the correlation window
    ts, dest, prev = client._pending_sv
    client._pending_sv = (ts - 60, dest, prev)
    client._dispatch(".E")
    assert client.routes[1] == 360  # kept — the .E is unrelated


# ── Reconnect backoff ──────────────────────────────────────────────────────


def test_backoff_doubles_and_caps() -> None:
    from custom_components.evertz_quartz.quartz_client import MAX_RECONNECT_DELAY

    client = make_client(reconnect_delay=5)
    delays = [client._next_reconnect_delay() for _ in range(7)]
    assert delays == [5, 10, 20, 40, 80, 120, 120]
    assert delays[-1] == MAX_RECONNECT_DELAY


def test_backoff_resets_on_option_update() -> None:
    client = make_client(reconnect_delay=5)
    client._next_reconnect_delay()
    client._next_reconnect_delay()
    client.update_options(reconnect_delay=7)
    assert client._next_reconnect_delay() == 7


# ── Mnemonic sweep abort ───────────────────────────────────────────────────


def _count_queries(writer: FakeWriter, prefix: str) -> int:
    return sum(chunk.count(prefix) for chunk in writer.commands)


async def test_mnemonic_sweep_aborts_after_consecutive_errors() -> None:
    from custom_components.evertz_quartz.quartz_client import SWEEP_BATCH

    client = make_client(max_sources=100)
    writer = FakeWriter()
    client._writer = writer
    client._connected = True

    orig_drain = writer.drain
    answered = 0

    async def drain_and_reject() -> None:
        nonlocal answered
        await orig_drain()
        # Simulate the concurrent reader dispatching an .E per query written
        total = _count_queries(writer, ".RT")
        for _ in range(total - answered):
            client._dispatch(".E")
        answered = total

    writer.drain = drain_and_reject
    await client._mnemonic_sweep(".RT", 100)

    # Aborted after the first batch — well short of the full 100-query storm
    assert _count_queries(writer, ".RT") == SWEEP_BATCH
    assert client.stats.mnemonic_rejected >= MNEMONIC_ABORT_AFTER
    assert client.stats.errors == []


async def test_mnemonic_sweep_completes_when_replies_arrive() -> None:
    client = make_client(max_sources=20)
    writer = FakeWriter()
    client._writer = writer
    client._connected = True

    orig_drain = writer.drain
    answered = 0

    async def drain_and_reply() -> None:
        nonlocal answered
        await orig_drain()
        total = _count_queries(writer, ".RT")
        for order in range(answered + 1, total + 1):
            client._dispatch(f".RAT{order},SRC-{order:03d}")
        answered = total

    writer.drain = drain_and_reply
    await client._mnemonic_sweep(".RT", 20)

    assert _count_queries(writer, ".RT") == 20
    assert len(client.source_names) == 20


async def test_query_all_routes_is_batched() -> None:
    from custom_components.evertz_quartz.quartz_client import SWEEP_BATCH

    client = make_client(max_destinations=20)
    writer = FakeWriter()
    client._writer = writer
    client._connected = True

    await client.query_all_routes()

    assert _count_queries(writer, ".IV") == 20
    assert client.stats.interrogate_sent == 20
    # ceil(20 / SWEEP_BATCH) drains, not 20
    assert len(writer.commands) == -(-20 // SWEEP_BATCH)


async def test_wait_interrogation_drain_returns_when_answered() -> None:
    client = make_client()
    client._interrogate_pending = 2
    client._dispatch(".AV1,5")
    client._dispatch(".AV2,6")
    await asyncio.wait_for(client.wait_interrogation_drain(), timeout=1)


async def test_wait_interrogation_drain_gives_up_on_stall() -> None:
    client = make_client()
    client._interrogate_pending = 3  # controller never answers
    await asyncio.wait_for(
        client.wait_interrogation_drain(timeout=10, stall=0.3), timeout=2
    )


def test_estimated_sync_seconds_scales_with_batches() -> None:
    small = make_client(max_destinations=32, max_sources=32, csv_loaded=True)
    big = make_client(max_destinations=512, max_sources=1164, csv_loaded=True)
    assert small.estimated_sync_seconds() >= 3
    # 512 destinations: 64 batches × 50 ms × 2 sweeps + 2 s grace ≈ 8 s
    assert big.estimated_sync_seconds() <= 12


# ── Optimistic routing ─────────────────────────────────────────────────────


async def test_route_sends_sv_and_updates_optimistically() -> None:
    calls: list = []
    client = make_client(route_callback=lambda *a: calls.append(a))
    client._writer = FakeWriter()
    client._connected = True

    ok = await client.route(1, 360)
    assert ok
    assert client._writer.commands == [".SVV001,360\r"]
    assert client.routes == {1: 360}
    assert client.stats.sv_sent == 1
    assert calls == [(1, 360, "V")]


async def test_route_levels_override() -> None:
    client = make_client()
    client._writer = FakeWriter()
    client._connected = True
    await client.route(2, 3, levels="VA")
    assert client._writer.commands == [".SVVA002,003\r"]


async def test_route_fails_when_disconnected() -> None:
    client = make_client()
    ok = await client.route(1, 360)
    assert not ok
    assert client.routes == {}


# ── End-to-end against a fake TCP router ──────────────────────────────────


@pytest.mark.usefixtures("socket_enabled")
async def test_connect_sweep_and_framing() -> None:
    """CR-only framing, concurrent sweep processing, .E storm suppression."""

    async def handle(reader, writer) -> None:
        try:
            while True:
                raw = await reader.readuntil(b"\r")
                line = raw.decode().strip()
                if line.startswith(".IV"):
                    dest = int(line[3:])
                    # Two real destinations; .E for the rest (over-provisioned)
                    reply = f".AV{dest},{100 + dest}\r" if dest <= 2 else ".E\r"
                    writer.write(reply.encode())
                elif line.startswith(".BI"):
                    writer.write(f".BA{line[3:]},0\r".encode())
                elif line.startswith((".RD", ".RT")):
                    writer.write(b".E\r")  # MAGNUM rejects mnemonic queries
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError, OSError):
            pass

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    synced = asyncio.Event()
    client = make_client(
        host="127.0.0.1",
        port=port,
        max_sources=50,
        max_destinations=5,
        sync_callback=synced.set,
    )
    await client.start()
    try:
        await asyncio.wait_for(synced.wait(), timeout=15)
        await asyncio.sleep(0.3)  # reply grace

        assert client.routes == {1: 101, 2: 102}
        assert client.locks == {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
        # Expected .E replies were attributed, not recorded as errors
        assert client.stats.interrogate_rejected == 3
        assert client.stats.mnemonic_rejected > 0
        assert client.stats.errors == []
        # The .RT sweep aborted early instead of sending all 50 queries
        assert client.stats.mnemonic_rejected < 55
    finally:
        await client.stop()
        server.close()
        await server.wait_closed()
