"""Tests for helpers — permission matrix and blocked-route notifications."""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.evertz_quartz.const import (
    CONF_READONLY_ALLOWED_USERS,
    CONF_READONLY_DESTINATIONS,
    DOMAIN,
    EVENT_ROUTE_BLOCKED,
)
from custom_components.evertz_quartz.helpers import (
    effective,
    notify_blocked_route,
    readonly_destinations,
    router_display_name,
    user_can_route,
)
from custom_components.evertz_quartz.quartz_client import QuartzClient


def make_entry(data: dict | None = None, options: dict | None = None) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        data=data or {"host": "router.local", "port": 6666},
        options=options or {},
    )


def test_effective_prefers_options_over_data() -> None:
    entry = make_entry(data={"levels": "V"}, options={"levels": "VA"})
    assert effective(entry, "levels", "X") == "VA"
    entry = make_entry(data={"levels": "V"})
    assert effective(entry, "levels", "X") == "V"
    entry = make_entry()
    assert effective(entry, "levels", "X") == "X"


def test_router_display_name_falls_back_to_host() -> None:
    assert router_display_name(make_entry({"router_name": "MY-ROUTER", "host": "h"})) == "MY-ROUTER"
    assert router_display_name(make_entry({"host": "router.local"})) == "router.local"
    assert router_display_name(MockConfigEntry(domain=DOMAIN, data={})) == "Unknown Router"


def test_readonly_destinations_parses_orders() -> None:
    entry = make_entry(options={CONF_READONLY_DESTINATIONS: ["1", "5"]})
    assert readonly_destinations(entry) == {1, 5}
    assert readonly_destinations(make_entry()) == set()


def test_user_can_route_matrix() -> None:
    entry = make_entry(
        options={
            CONF_READONLY_DESTINATIONS: ["2"],
            CONF_READONLY_ALLOWED_USERS: ["user-a"],
        }
    )
    # Non-read-only destination: everyone may route, even without user context
    assert user_can_route(entry, 1, None)
    assert user_can_route(entry, 1, "user-b")
    # Read-only destination: only allowed users
    assert user_can_route(entry, 2, "user-a")
    assert not user_can_route(entry, 2, "user-b")
    # No user context (automations/scripts) is always blocked on read-only
    assert not user_can_route(entry, 2, None)


def test_user_can_route_readonly_with_empty_allowlist() -> None:
    entry = make_entry(options={CONF_READONLY_DESTINATIONS: ["2"]})
    assert not user_can_route(entry, 2, "user-a")


def test_detection_status_over_provisioned() -> None:
    from custom_components.evertz_quartz.helpers import detection_status

    client = QuartzClient(
        host="router.local", port=6666, max_sources=100, max_destinations=32, levels="V"
    )
    client.max_dst_order_seen = 4
    client.max_src_order_seen = 120

    status = detection_status(make_entry(), client)
    assert status.over_provisioned
    assert status.configured_destinations == 32
    assert status.detected_destinations == 4
    assert status.suggested_max_destinations == 4
    assert status.suggested_max_sources == 120  # grow-only hint


def test_detection_status_no_data() -> None:
    from custom_components.evertz_quartz.helpers import detection_status

    client = QuartzClient(
        host="router.local", port=6666, max_sources=100, max_destinations=32, levels="V"
    )
    status = detection_status(make_entry(), client)
    assert not status.over_provisioned  # detected 0 = could not detect
    assert status.suggested_max_destinations == 32


def test_subscribe_listener_unsubscribes_safely() -> None:
    from custom_components.evertz_quartz.helpers import subscribe_listener

    listeners: list = []
    cb = lambda: None  # noqa: E731
    unsub = subscribe_listener(listeners, cb)
    assert listeners == [cb]
    unsub()
    assert listeners == []
    unsub()  # second call after a list rebuild must not raise
    assert listeners == []


async def test_notify_blocked_route_fires_event_and_notification(hass) -> None:
    entry = make_entry(data={"router_name": "MY-ROUTER", "host": "router.local"})
    client = QuartzClient(
        host="router.local", port=6666, max_sources=4, max_destinations=2, levels="V"
    )
    client.destination_names[1] = "DEST-A"
    client.source_names[3] = "SRC-003"

    notifications = async_mock_service(hass, "persistent_notification", "create")
    events = []
    hass.bus.async_listen(EVENT_ROUTE_BLOCKED, events.append)

    notify_blocked_route(
        hass, entry, client,
        reason="read_only", dest_order=1, src_order=3,
        user_id="user-b", origin="service",
    )
    await hass.async_block_till_done()

    assert len(events) == 1
    data = events[0].data
    assert data["router"] == "MY-ROUTER"
    assert data["entry_id"] == entry.entry_id
    assert data["reason"] == "read_only"
    assert data["origin"] == "service"
    assert data["action"] == "route"
    assert data["destination"] == 1
    assert data["destination_name"] == "DEST-A"
    assert data["source"] == 3
    assert data["source_name"] == "SRC-003"
    assert data["user_id"] == "user-b"

    assert len(notifications) == 1
    assert "read-only" in notifications[0].data["message"]


async def test_notify_blocked_route_locked_and_cross_namespace(hass) -> None:
    entry = make_entry()
    client = QuartzClient(
        host="router.local", port=6666, max_sources=4, max_destinations=2, levels="V"
    )
    client.source_namespaces[3] = "XY"
    client.destination_namespaces[1] = "VP"

    notifications = async_mock_service(hass, "persistent_notification", "create")
    events = []
    hass.bus.async_listen(EVENT_ROUTE_BLOCKED, events.append)

    notify_blocked_route(
        hass, entry, client, reason="locked", dest_order=1, src_order=3, origin="select",
    )
    notify_blocked_route(
        hass, entry, client, reason="cross_namespace", dest_order=1, src_order=3,
        origin="service",
    )
    await hass.async_block_till_done()

    assert sorted(e.data["reason"] for e in events) == ["cross_namespace", "locked"]
    assert len(notifications) == 2
    messages = " | ".join(n.data["message"] for n in notifications)
    assert "locked" in messages
    assert "VP" in messages


async def test_notify_blocked_lock_action_wording(hass) -> None:
    entry = make_entry()
    client = QuartzClient(
        host="router.local", port=6666, max_sources=4, max_destinations=2, levels="V"
    )
    notifications = async_mock_service(hass, "persistent_notification", "create")

    notify_blocked_route(
        hass, entry, client, reason="read_only", dest_order=1,
        origin="lock", action="unlock",
    )
    await hass.async_block_till_done()

    assert len(notifications) == 1
    assert "Unlock Blocked" in notifications[0].data["title"]
