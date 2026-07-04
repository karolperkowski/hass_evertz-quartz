"""Tests for options-flow diff builders (resize + CSV import summaries)."""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.evertz_quartz.const import (
    CONF_READONLY_DESTINATIONS,
    DOMAIN,
)
from custom_components.evertz_quartz.csv_parser import parse_csv
from custom_components.evertz_quartz.options_flow import (
    _build_count_diff,
    _build_csv_diff,
)


def make_entry(data: dict | None = None, options: dict | None = None) -> MockConfigEntry:
    base = {
        "host": "router.local",
        "port": 6666,
        "max_sources": 32,
        "max_destinations": 8,
        "csv_loaded": False,
    }
    base.update(data or {})
    return MockConfigEntry(domain=DOMAIN, data=base, options=options or {})


def test_count_diff_no_change() -> None:
    diff = _build_count_diff(make_entry(), 32, 8)
    assert diff["changes"] == []


def test_count_diff_grow_and_shrink() -> None:
    diff = _build_count_diff(make_entry(), 64, 4)
    assert any("32 → 64" in c for c in diff["changes"])
    assert any("8 → 4" in c for c in diff["changes"])
    # Growth explains new placeholder sources; shrink warns about removal
    assert any("Source N" in n for n in diff["notes"])
    assert any("will be removed" in n for n in diff["notes"])


def test_count_diff_preserves_csv_note() -> None:
    diff = _build_count_diff(make_entry({"csv_loaded": True}), 64, 8)
    assert any("CSV names are preserved" in n for n in diff["notes"])


CSV_TEXT = (
    "Device Short Name,Src or Dst,Port Number,Global Name,Hidden?,Order\n"
    "VP,SRC,1,SRC-001,0,1\n"
    "VP,SRC,500,SRC-TIE,0,2\n"
    "VP,SRC,3,SRC-HID,1,3\n"
    "VP,DST,323,DEST-A,0,1\n"
)


def test_csv_diff_reports_changes_and_hidden() -> None:
    result = parse_csv(CSV_TEXT)
    diff = _build_csv_diff(make_entry(), result)
    assert any("Max Sources: 32 → 3" in c for c in diff["changes"])
    assert any("Max Destinations: 8 → 1" in c for c in diff["changes"])
    assert any("Hidden ports: 1 src" in c for c in diff["changes"])
    # Order ≠ Port rows produce the non-contiguous warning
    assert any("Non-contiguous" in w for w in diff["warnings"])


def test_csv_diff_warns_about_readonly_orders() -> None:
    result = parse_csv(CSV_TEXT)
    entry = make_entry(options={CONF_READONLY_DESTINATIONS: ["1"]})
    diff = _build_csv_diff(entry, result)
    assert any("Read-only destinations are keyed by Order" in w for w in diff["warnings"])
