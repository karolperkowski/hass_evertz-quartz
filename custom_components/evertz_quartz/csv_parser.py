"""
Flexible CSV parser for Evertz router profile exports.

Key distinction:
  Order       = sequential display index within the profile (1, 2, 3 … N)
                → determines how many entities HA creates (max_sources / max_destinations)
  Port Number = Quartz crosspoint address used in .SV / .UV commands
                → can differ from Order when tieline / remote sources use non-contiguous ports

max_sources and max_destinations are therefore max(Order), not max(Port Number).

source_port_map  / destination_port_map store {order: port_number} so that
select entities can route to the correct Quartz port even when Order ≠ Port.
source_names / destination_names are keyed by Port Number so they stay
compatible with the live .RT / .RD mnemonic responses from the router.

Priority order (first match wins):
  1. MAGNUM profile_availability  (Device Short Name, Src or Dst, Port Number, Global Name, Hidden?, Order)
  2. Generic alias export          (Type, Number, Name rows)
  3. Two-column with header        (Source, Destination columns)
  4. Shorthand numbers             (32,32 / 32/16 / sources=32 destinations=16)
  5. Sectioned list                (Sources / Destinations section headers)
"""

from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass, field

_LOGGER = logging.getLogger(__name__)


@dataclass
class ParseResult:
    # How many entities to create
    max_sources: int
    max_destinations: int
    # Keyed by Port Number (Quartz address) — compatible with live .RT/.RD responses
    source_names: dict[int, str]
    destination_names: dict[int, str]
    # Keyed by Order (entity index) → Port Number (Quartz address)
    # Identity mapping ({1:1, 2:2, …}) when Order == Port for all rows
    source_port_map: dict[int, int]
    destination_port_map: dict[int, int]
    format_detected: str
    hidden_sources: int = 0
    hidden_destinations: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def has_port_gaps(self) -> bool:
        """True when any Order ≠ Port Number (non-contiguous port numbering)."""
        for order, port in self.source_port_map.items():
            if order != port:
                return True
        for order, port in self.destination_port_map.items():
            if order != port:
                return True
        return False

    @property
    def summary(self) -> str:
        parts = [f"{self.max_sources} sources", f"{self.max_destinations} destinations"]
        if self.has_port_gaps:
            parts.append("non-contiguous port numbering")
        if self.hidden_sources or self.hidden_destinations:
            parts.append(
                f"{self.hidden_sources} hidden src, {self.hidden_destinations} hidden dst"
            )
        return ", ".join(parts)


def parse_csv(text: str) -> ParseResult | None:
    """Parse router profile CSV. Returns ParseResult or None if unrecognised."""
    text = text.strip()
    if not text:
        return None

    for parser in (
        _parse_magnum_profile,
        _parse_alias_export,
        _parse_two_column,
        _parse_shorthand,
        _parse_single_section,
    ):
        result = parser(text)
        if result is not None:
            _LOGGER.debug("CSV parsed as '%s': %s", result.format_detected, result.summary)
            if result.warnings:
                _LOGGER.warning("CSV parse warnings: %s", result.warnings)
            return result

    _LOGGER.warning("Could not parse CSV — no matching format found")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Format 1: MAGNUM profile_availability
#
# Header: Device Short Name,Src or Dst,Port Number,Global Name,Hidden?,Order
# Cols:   0                 1          2            3           4        5
#
# Order      = sequential profile index — entity count
# Port Number = Quartz crosspoint address — used in .SV/.UV
# ─────────────────────────────────────────────────────────────────────────────

_MAGNUM_HEADER_KEYWORDS = {"src or dst", "port number", "global name", "order"}


def _parse_magnum_profile(text: str) -> ParseResult | None:
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return None

    header = [c.strip().lower() for c in rows[0]]
    if not _MAGNUM_HEADER_KEYWORDS.issubset(set(header)):
        return None

    try:
        type_col   = next(i for i, h in enumerate(header) if h == "src or dst")
        port_col   = next(i for i, h in enumerate(header) if h == "port number")
        name_col   = next(i for i, h in enumerate(header) if h == "global name")
        order_col  = next(i for i, h in enumerate(header) if h == "order")
        hidden_col = next((i for i, h in enumerate(header) if "hidden" in h), None)
    except StopIteration:
        return None

    src_names: dict[int, str] = {}          # port → name
    dst_names: dict[int, str] = {}          # port → name
    src_port_map: dict[int, int] = {}       # order → port
    dst_port_map: dict[int, int] = {}       # order → port
    hidden_src = hidden_dst = 0
    warnings: list[str] = []

    for row_num, row in enumerate(rows[1:], start=2):
        if not row or not any(c.strip() for c in row):
            continue
        row = [c.strip() for c in row]

        try:
            kind   = row[type_col].upper()
            port   = int(row[port_col])
            name   = row[name_col] if name_col < len(row) else ""
            order  = int(row[order_col]) if order_col < len(row) else port
            hidden = int(row[hidden_col]) if hidden_col is not None and hidden_col < len(row) else 0
        except (IndexError, ValueError) as err:
            warnings.append(f"Row {row_num}: skipped ({err})")
            continue

        if kind == "SRC":
            if hidden:
                hidden_src += 1
            src_names[order] = name or f"Source {order}"   # keyed by Order (MAGNUM uses Order)
            src_port_map[order] = port                      # kept for reference only
        elif kind in ("DST", "DEST", "DESTINATION"):
            if hidden:
                hidden_dst += 1
            dst_names[order] = name or f"Destination {order}"  # keyed by Order
            dst_port_map[order] = port
        else:
            warnings.append(f"Row {row_num}: unknown type {kind!r}, skipped")

    if not src_names and not dst_names:
        return None

    max_src = max(src_port_map.keys()) if src_port_map else 0
    max_dst = max(dst_port_map.keys()) if dst_port_map else 0

    return ParseResult(
        max_sources=max_src,
        max_destinations=max_dst,
        source_names=src_names,
        destination_names=dst_names,
        source_port_map=src_port_map,
        destination_port_map=dst_port_map,
        format_detected="MAGNUM profile_availability",
        hidden_sources=hidden_src,
        hidden_destinations=hidden_dst,
        warnings=warnings,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Format 2: Generic alias export  (Type,Number,Name rows)
# Number is treated as both Order and Port (no gap information available)
# ─────────────────────────────────────────────────────────────────────────────

def _parse_alias_export(text: str) -> ParseResult | None:
    src_names: dict[int, str] = {}
    dst_names: dict[int, str] = {}
    warnings: list[str] = []
    matched = 0

    for row in csv.reader(io.StringIO(text)):
        if not row:
            continue
        row = [c.strip() for c in row]
        kind = row[0].upper()
        if kind in ("TYPE", "SOURCE", "DESTINATION", "#", ""):
            continue
        if kind in ("SRC", "SOURCE", "S"):
            try:
                num = int(row[1])
                src_names[num] = row[2] if len(row) > 2 else f"Source {num}"
                matched += 1
            except (IndexError, ValueError):
                warnings.append(f"Skipped malformed SRC row: {row}")
        elif kind in ("DST", "DESTINATION", "D", "DEST"):
            try:
                num = int(row[1])
                dst_names[num] = row[2] if len(row) > 2 else f"Destination {num}"
                matched += 1
            except (IndexError, ValueError):
                warnings.append(f"Skipped malformed DST row: {row}")

    if matched == 0:
        return None

    return ParseResult(
        max_sources=max(src_names.keys()) if src_names else 0,
        max_destinations=max(dst_names.keys()) if dst_names else 0,
        source_names=src_names,
        destination_names=dst_names,
        source_port_map={n: n for n in src_names},
        destination_port_map={n: n for n in dst_names},
        format_detected="alias export",
        warnings=warnings,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Format 3: Two-column CSV
# ─────────────────────────────────────────────────────────────────────────────

def _parse_two_column(text: str) -> ParseResult | None:
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if r]
    if len(rows) < 2:
        return None

    header = [c.strip().lower() for c in rows[0]]
    src_col = next((i for i, h in enumerate(header) if h in ("source","src","input","in")), None)
    dst_col = next((i for i, h in enumerate(header) if h in ("destination","dst","dest","output","out")), None)
    if src_col is None or dst_col is None:
        return None

    src_names: dict[int, str] = {}
    dst_names: dict[int, str] = {}
    for i, row in enumerate(rows[1:], start=1):
        row = [c.strip() for c in row]
        if src_col < len(row) and row[src_col]:
            src_names[i] = row[src_col]
        if dst_col < len(row) and row[dst_col]:
            dst_names[i] = row[dst_col]

    if not src_names and not dst_names:
        return None

    return ParseResult(
        max_sources=max(src_names.keys()) if src_names else 0,
        max_destinations=max(dst_names.keys()) if dst_names else 0,
        source_names=src_names,
        destination_names=dst_names,
        source_port_map={n: n for n in src_names},
        destination_port_map={n: n for n in dst_names},
        format_detected="two-column CSV",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Format 4: Shorthand numbers — no name or port info, identity mapping
# ─────────────────────────────────────────────────────────────────────────────

def _parse_shorthand(text: str) -> ParseResult | None:
    src_m = re.search(r"(?:sources?|src|inputs?)\s*[=:]\s*(\d+)", text, re.I)
    dst_m = re.search(r"(?:destinations?|dst|dest|outputs?)\s*[=:]\s*(\d+)", text, re.I)
    if src_m and dst_m:
        max_s = int(src_m.group(1))
        max_d = int(dst_m.group(1))
        return ParseResult(
            max_sources=max_s, max_destinations=max_d,
            source_names={}, destination_names={},
            source_port_map={n: n for n in range(1, max_s + 1)},
            destination_port_map={n: n for n in range(1, max_d + 1)},
            format_detected="shorthand key=value",
        )

    m = re.fullmatch(r"\s*(\d+)\s*[,/ x×]\s*(\d+)\s*", text, re.I)
    if m:
        max_s, max_d = int(m.group(1)), int(m.group(2))
        return ParseResult(
            max_sources=max_s, max_destinations=max_d,
            source_names={}, destination_names={},
            source_port_map={n: n for n in range(1, max_s + 1)},
            destination_port_map={n: n for n in range(1, max_d + 1)},
            format_detected="shorthand NxN",
        )

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Format 5: Sectioned list
# ─────────────────────────────────────────────────────────────────────────────

def _parse_single_section(text: str) -> ParseResult | None:
    src_names: dict[int, str] = {}
    dst_names: dict[int, str] = {}
    current: str | None = None
    matched = 0

    src_headers = {"sources","source","src","inputs","input"}
    dst_headers = {"destinations","destination","dst","dest","outputs","output"}

    for row in csv.reader(io.StringIO(text)):
        if not row:
            continue
        first = row[0].strip().lower()
        if first in src_headers:
            current = "src"; continue
        if first in dst_headers:
            current = "dst"; continue
        if current is None:
            continue

        try:
            num = int(row[0].strip())
            name = row[1].strip() if len(row) > 1 else ""
        except ValueError:
            num = len(src_names if current == "src" else dst_names) + 1
            name = row[0].strip()

        if current == "src":
            src_names[num] = name or f"Source {num}"
        else:
            dst_names[num] = name or f"Destination {num}"
        matched += 1

    if matched == 0:
        return None

    return ParseResult(
        max_sources=max(src_names.keys()) if src_names else 0,
        max_destinations=max(dst_names.keys()) if dst_names else 0,
        source_names=src_names,
        destination_names=dst_names,
        source_port_map={n: n for n in src_names},
        destination_port_map={n: n for n in dst_names},
        format_detected="sectioned list",
    )
