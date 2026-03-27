"""
Flexible CSV parser for Evertz router profile exports.

Supports several formats that may be exported from EQX Server / MAGNUM:

Format 1 — Evertz alias export (most structured):
    Type,Number,Name[,Alias,...]
    SRC,1,CAM-A
    SRC,2,CAM-B
    DST,1,MON-1
    DST,2,MON-2

Format 2 — Two-column with header:
    Source,Destination
    CAM-A,MON-1
    CAM-B,MON-2

Format 3 — Two-number shorthand (entered manually):
    32,32
    32/32
    sources=32 destinations=32

Format 4 — Single-column numbered list (sources then destinations in two sections):
    Sources
    1,CAM-A
    2,CAM-B
    Destinations
    1,MON-1

Format 5 — Plain number list (one entry per row, highest number = count):
    1
    2
    ...
    32
"""

from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass

_LOGGER = logging.getLogger(__name__)


@dataclass
class ParseResult:
    max_sources: int
    max_destinations: int
    source_names: dict[int, str]        # optional name mapping
    destination_names: dict[int, str]   # optional name mapping
    format_detected: str
    warnings: list[str]


def parse_csv(text: str) -> ParseResult | None:
    """
    Parse router profile CSV text and return (max_sources, max_destinations).

    Returns None if the text cannot be interpreted as a valid profile.
    """
    text = text.strip()
    if not text:
        return None

    for parser in (
        _parse_alias_export,
        _parse_two_column,
        _parse_shorthand,
        _parse_single_section,
    ):
        result = parser(text)
        if result is not None:
            _LOGGER.debug(
                "CSV parsed as %s: %d sources, %d destinations",
                result.format_detected,
                result.max_sources,
                result.max_destinations,
            )
            return result

    _LOGGER.warning("Could not parse CSV — no matching format found")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Format 1: Evertz alias export
#   Type,Number,Name[,alias,...]
#   SRC,1,CAM-A,...
#   DST,1,MON-1,...
# ─────────────────────────────────────────────────────────────────────────────

def _parse_alias_export(text: str) -> ParseResult | None:
    src_names: dict[int, str] = {}
    dst_names: dict[int, str] = {}
    warnings: list[str] = []

    reader = csv.reader(io.StringIO(text))
    matched = 0

    for row in reader:
        if not row:
            continue
        row = [c.strip() for c in row]
        kind = row[0].upper()

        if kind in ("TYPE", "SOURCE", "DESTINATION", "#", ""):
            continue  # skip header rows

        if kind in ("SRC", "SOURCE", "S"):
            try:
                num = int(row[1])
                name = row[2] if len(row) > 2 else f"Source {num}"
                src_names[num] = name
                matched += 1
            except (IndexError, ValueError):
                warnings.append(f"Skipped malformed SRC row: {row}")

        elif kind in ("DST", "DESTINATION", "D", "DEST"):
            try:
                num = int(row[1])
                name = row[2] if len(row) > 2 else f"Destination {num}"
                dst_names[num] = name
                matched += 1
            except (IndexError, ValueError):
                warnings.append(f"Skipped malformed DST row: {row}")

    if matched == 0 or (not src_names and not dst_names):
        return None

    max_src = max(src_names.keys()) if src_names else 0
    max_dst = max(dst_names.keys()) if dst_names else 0

    if max_src == 0 and max_dst == 0:
        return None

    return ParseResult(
        max_sources=max_src,
        max_destinations=max_dst,
        source_names=src_names,
        destination_names=dst_names,
        format_detected="Evertz alias export",
        warnings=warnings,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Format 2: Two-column CSV with Source,Destination header
#   Source,Destination
#   CAM-A,MON-1
# ─────────────────────────────────────────────────────────────────────────────

def _parse_two_column(text: str) -> ParseResult | None:
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if r]
    if len(rows) < 2:
        return None

    header = [c.strip().lower() for c in rows[0]]
    src_keywords = ("source", "src", "input", "in")
    dst_keywords = ("destination", "dst", "dest", "output", "out")

    src_col = next((i for i, h in enumerate(header) if h in src_keywords), None)
    dst_col = next((i for i, h in enumerate(header) if h in dst_keywords), None)

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
        format_detected="two-column CSV",
        warnings=[],
    )


# ─────────────────────────────────────────────────────────────────────────────
# Format 3: Shorthand — user just types the counts
#   "32,32"  or  "32/32"  or  "sources=32 destinations=32"  or just "32 32"
# ─────────────────────────────────────────────────────────────────────────────

def _parse_shorthand(text: str) -> ParseResult | None:
    # Named: sources=32 destinations=32  (in any order, any separator)
    src_m = re.search(r"(?:sources?|src|inputs?)\s*[=:]\s*(\d+)", text, re.I)
    dst_m = re.search(r"(?:destinations?|dst|dest|outputs?)\s*[=:]\s*(\d+)", text, re.I)
    if src_m and dst_m:
        return ParseResult(
            max_sources=int(src_m.group(1)),
            max_destinations=int(dst_m.group(1)),
            source_names={},
            destination_names={},
            format_detected="shorthand key=value",
            warnings=[],
        )

    # Two numbers separated by comma, slash, space, or x
    m = re.fullmatch(r"\s*(\d+)\s*[,/ x×]\s*(\d+)\s*", text, re.I)
    if m:
        return ParseResult(
            max_sources=int(m.group(1)),
            max_destinations=int(m.group(2)),
            source_names={},
            destination_names={},
            format_detected="shorthand NxN",
            warnings=[],
        )

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Format 4: Sectioned list
#   Sources       ← section header
#   1,CAM-A
#   2,CAM-B
#   Destinations  ← section header
#   1,MON-1
# ─────────────────────────────────────────────────────────────────────────────

def _parse_single_section(text: str) -> ParseResult | None:
    src_names: dict[int, str] = {}
    dst_names: dict[int, str] = {}
    current_section: str | None = None
    matched = 0

    src_headers = {"sources", "source", "src", "inputs", "input"}
    dst_headers = {"destinations", "destination", "dst", "dest", "outputs", "output"}

    reader = csv.reader(io.StringIO(text))
    for row in reader:
        if not row:
            continue
        first = row[0].strip().lower()

        if first in src_headers:
            current_section = "src"
            continue
        if first in dst_headers:
            current_section = "dst"
            continue

        if current_section is None:
            continue

        # Try to parse "num,name" or just "name"
        try:
            num = int(row[0].strip())
            name = row[1].strip() if len(row) > 1 else ""
        except ValueError:
            # No leading number — use incrementing counter
            if current_section == "src":
                num = len(src_names) + 1
            else:
                num = len(dst_names) + 1
            name = row[0].strip()

        if current_section == "src":
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
        format_detected="sectioned list",
        warnings=[],
    )
