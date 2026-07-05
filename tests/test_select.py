"""Tests for the destination select option building — dedup, hidden, namespaces."""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.evertz_quartz.const import DOMAIN
from custom_components.evertz_quartz.quartz_client import QuartzClient
from custom_components.evertz_quartz.select import QuartzDestinationSelect


def make_select(client: QuartzClient, order: int = 1) -> QuartzDestinationSelect:
    entry = MockConfigEntry(
        domain=DOMAIN, data={"host": "router.local", "port": 6666}
    )
    return QuartzDestinationSelect(entry=entry, client=client, order=order)


def make_client(max_sources: int = 5) -> QuartzClient:
    return QuartzClient(
        host="router.local", port=6666, max_sources=max_sources,
        max_destinations=2, levels="V",
    )


def test_options_fallback_names_without_csv() -> None:
    select = make_select(make_client(3))
    assert select.options == ["Source 1", "Source 2", "Source 3"]


def test_duplicate_names_get_order_suffix_and_resolve_unambiguously() -> None:
    client = make_client(3)
    client.source_names.update({1: "CAM", 2: "CAM", 3: "VTR"})
    select = make_select(client)
    assert select.options == ["CAM (Order 1)", "CAM (Order 2)", "VTR"]
    assert select._label_to_order == {"CAM (Order 1)": 1, "CAM (Order 2)": 2, "VTR": 3}


def test_hidden_sources_excluded_from_options() -> None:
    client = make_client(3)
    client.source_names.update({1: "SRC-001", 2: "SRC-HID", 3: "SRC-003"})
    client.hidden_sources = {2}
    select = make_select(client)
    assert select.options == ["SRC-001", "SRC-003"]


def test_namespace_filtering() -> None:
    client = make_client(4)
    client.source_names.update({1: "A", 2: "B", 3: "C", 4: "D"})
    client.source_namespaces.update({1: "VP", 2: "XY", 3: "VP", 4: "XY"})
    client.destination_namespaces[1] = "VP"
    select = make_select(client, order=1)
    assert select.options == ["A", "C"]


def test_current_option_uses_deduped_label() -> None:
    client = make_client(3)
    client.source_names.update({1: "CAM", 2: "CAM"})
    client.routes[1] = 2
    select = make_select(client)
    assert select.current_option == "CAM (Order 2)"


def test_current_option_falls_back_for_hidden_source() -> None:
    client = make_client(3)
    client.source_names.update({1: "SRC-001", 2: "SRC-HID"})
    client.hidden_sources = {2}
    client.routes[1] = 2
    select = make_select(client)
    # Hidden source routed by another controller — still displayed by name
    assert select.current_option == "SRC-HID"


def test_cache_invalidation_on_name_update() -> None:
    client = make_client(2)
    select = make_select(client)
    assert select.options == ["Source 1", "Source 2"]
    client.source_names[1] = "NEW-NAME"
    # Stale until invalidated (that's the cache working)
    assert select.options == ["Source 1", "Source 2"]
    select._invalidate_options()
    assert select.options == ["NEW-NAME", "Source 2"]
