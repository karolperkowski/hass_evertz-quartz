"""Tests for csv_parser — all 5 formats, hidden rows, Order≠Port, malformed rows."""

from __future__ import annotations

from custom_components.evertz_quartz.csv_parser import parse_csv

MAGNUM_CSV = """\
Device Short Name,Src or Dst,Port Number,Global Name,Hidden?,Order
VP,SRC,1,SRC-001,0,1
VP,SRC,500,SRC-TIE,0,2
VP,SRC,3,SRC-HID,1,3
VP,DST,323,DEST-A,0,1
XY,DST,4,DEST-B,1,2
"""


def test_magnum_profile_basics() -> None:
    result = parse_csv(MAGNUM_CSV)
    assert result is not None
    assert result.format_detected == "MAGNUM profile_availability"
    assert result.max_sources == 3
    assert result.max_destinations == 2
    # Names keyed by Order, not Port Number
    assert result.source_names == {1: "SRC-001", 2: "SRC-TIE", 3: "SRC-HID"}
    assert result.destination_names == {1: "DEST-A", 2: "DEST-B"}
    # Port maps keyed by Order → Quartz port
    assert result.source_port_map == {1: 1, 2: 500, 3: 3}
    assert result.destination_port_map == {1: 323, 2: 4}
    # Namespaces from Device Short Name
    assert result.source_namespaces == {1: "VP", 2: "VP", 3: "VP"}
    assert result.destination_namespaces == {1: "VP", 2: "XY"}


def test_magnum_hidden_rows_kept_but_flagged() -> None:
    """Hidden rows stay in the maps (MAGNUM still uses their Orders)."""
    result = parse_csv(MAGNUM_CSV)
    assert result.hidden_sources == 1
    assert result.hidden_destinations == 1
    assert result.hidden_source_orders == [3]
    assert result.hidden_destination_orders == [2]
    # Still present in the name/port maps
    assert 3 in result.source_names
    assert 2 in result.destination_names


def test_magnum_port_gaps_detected() -> None:
    result = parse_csv(MAGNUM_CSV)
    assert result.has_port_gaps  # Order 2 → Port 500


def test_magnum_no_port_gaps() -> None:
    csv_text = (
        "Device Short Name,Src or Dst,Port Number,Global Name,Hidden?,Order\n"
        "VP,SRC,1,SRC-001,0,1\n"
        "VP,DST,1,DEST-A,0,1\n"
    )
    result = parse_csv(csv_text)
    assert result is not None
    assert not result.has_port_gaps


def test_magnum_malformed_rows_warn_and_skip() -> None:
    csv_text = (
        "Device Short Name,Src or Dst,Port Number,Global Name,Hidden?,Order\n"
        "VP,SRC,not_a_number,SRC-001,0,1\n"
        "VP,WHAT,2,SRC-002,0,2\n"
        "VP,SRC,3,SRC-003,0,3\n"
    )
    result = parse_csv(csv_text)
    assert result is not None
    assert result.source_names == {3: "SRC-003"}
    assert len(result.warnings) == 2


def test_alias_export() -> None:
    csv_text = "SRC,1,SRC-001\nSRC,2,SRC-002\nDST,1,DEST-A\n"
    result = parse_csv(csv_text)
    assert result is not None
    assert result.format_detected == "alias export"
    assert result.max_sources == 2
    assert result.max_destinations == 1
    assert result.source_names[2] == "SRC-002"
    # Identity port maps — no gap information in this format
    assert result.source_port_map == {1: 1, 2: 2}


def test_two_column() -> None:
    csv_text = "Source,Destination\nSRC-001,DEST-A\nSRC-002,\n"
    result = parse_csv(csv_text)
    assert result is not None
    assert result.format_detected == "two-column CSV"
    assert result.source_names == {1: "SRC-001", 2: "SRC-002"}
    assert result.destination_names == {1: "DEST-A"}


def test_shorthand_key_value() -> None:
    result = parse_csv("sources=32 destinations=16")
    assert result is not None
    assert result.format_detected == "shorthand key=value"
    assert result.max_sources == 32
    assert result.max_destinations == 16
    assert result.source_names == {}


def test_shorthand_nxn() -> None:
    result = parse_csv("32x16")
    assert result is not None
    assert result.format_detected == "shorthand NxN"
    assert result.max_sources == 32
    assert result.max_destinations == 16


def test_sectioned_list() -> None:
    csv_text = "Sources\n1,SRC-001\n2,SRC-002\nDestinations\n1,DEST-A\n"
    result = parse_csv(csv_text)
    assert result is not None
    assert result.format_detected == "sectioned list"
    assert result.source_names == {1: "SRC-001", 2: "SRC-002"}
    assert result.destination_names == {1: "DEST-A"}


def test_empty_and_unparseable() -> None:
    assert parse_csv("") is None
    assert parse_csv("   \n  ") is None
    assert parse_csv("complete nonsense with no structure whatsoever") is None
