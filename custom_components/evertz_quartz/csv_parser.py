"""
Flexible CSV parser for Evertz router profile exports.

Priority order (first match wins):

Format 1 — MAGNUM profile_availability export (canonical format):
    Device Short Name,Src or Dst,Port Number,Global Name,Hidden?,Order
    VP,SRC,1,CAM-A,0,1
    VP,DST,1,MON-1,0,1

    Port Number is the Quartz crosspoint number used in .SV/.UV commands.
    Hidden?=1 ports exist in the matrix but are excluded from the profile.
    max_sources/max_destinations = highest Port Number seen.

Format 2 — Generic alias export (Type,Number,Name rows):
    Type,Number,Name[,Alias,...]
    SRC,1,CAM-A
    DST,1,MON-1

Format 3 — Two-column with Source/Destination header:
    Source,Destination
    CAM-A,MON-1

Format 4 — Shorthand numbers:
    32,32  /  32/16  /  64x128  /  sources=32 destinations=16

Format 5 — Sectioned list:
    Sources
    1,CAM-A
    Destinations
    1,MON-1
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
    max_sources: int
    max_destinations: int
    source_names: dict[int, str]
    destination_names: dict[int, str]
    format_detected: str
    hidden_sources: int = 0       # count of Hidden?=1 SRC rows
    hidden_destinations: int = 0  # count of Hidden?=1 DST rows
    warnings: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        parts = [
            f"{self.max_sources} sources",
            f"{self.max_destinations} destinations",
        ]
        if self.hidden_sources or self.hidden_destinations:
            parts.append(
                f"({self.hidden_sources} hidden src, {self.hidden_destinations} hidden dst)"
            )
        return ", ".join(parts)


def parse_csv(text: str) -> ParseResult | None:
    """
    Parse router profile CSV text.

    Returns ParseResult or None if the text cannot be interpreted.
    """
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
            _LOGGER.debug(
                "CSV parsed as '%s': %s",
                result.format_detected,
                result.summary,
            )
            if result.warnings:
                _LOGGER.warning("CSV parse warnings: %s", result.warnings)
            return result

    _LOGGER.warning("Could not parse CSV — no matching format found")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Format 1: MAGNUM profile_availability export
#
# Header: Device Short Name,Src or Dst,Port Number,Global Name,Hidden?,Order
# Cols:   0                 1          2            3           4        5
#
# Port Number = Quartz crosspoint number (used in .SV / .UV commands)
# Hidden? = 0 (in profile) or 1 (exists on matrix but excluded from profile)
# Order = sequential display index, NOT the Quartz port number
# ─────────────────────────────────────────────────────────────────────────────

_MAGNUM_HEADER_KEYWORDS = {"src or dst", "port number", "global name"}


def _parse_magnum_profile(text: str) -> ParseResult | None:
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows:
        return None

    # Detect header row — must contain the canonical MAGNUM column names
    header = [c.strip().lower() for c in rows[0]]
    if not _MAGNUM_HEADER_KEYWORDS.issubset(set(header)):
        return None

    try:
        type_col = next(i for i, h in enumerate(header) if h == "src or dst")
        port_col = next(i for i, h in enumerate(header) if h == "port number")
        name_col = next(i for i, h in enumerate(header) if h == "global name")
        # Hidden is optional — default to 0 (not hidden) if absent
        hidden_col = next((i for i, h in enumerate(header) if "hidden" in h), None)
    except StopIteration:
        return None

    src_names: dict[int, str] = {}
    dst_names: dict[int, str] = {}
    hidden_src = 0
    hidden_dst = 0
    warnings: list[str] = []

    for row_num, row in enumerate(rows[1:], start=2):
        if not row or not any(c.strip() for c in row):
            continue
        row = [c.strip() for c in row]

        try:
            kind = row[type_col].upper()
            port = int(row[port_col])
            name = row[name_col] if name_col < len(row) else ""
            hidden = int(row[hidden_col]) if hidden_col is not None and hidden_col < len(row) else 0
        except (IndexError, ValueError) as err:
            warnings.append(f"Row {row_num}: skipped ({err})")
            continue

        if kind == "SRC":
            if hidden:
                hidden_src += 1
            src_names[port] = name or f"Source {port}"
        elif kind in ("DST", "DEST", "DESTINATION"):
            if hidden:
                hidden_dst += 1
            dst_names[port] = name or f"Destination {port}"
        else:
            warnings.append(f"Row {row_num}: unknown type {kind!r}, skipped")

    if not src_names and not dst_names:
        return None

    max_src = max(src_names.keys()) if src_names else 0
    max_dst = max(dst_names.keys()) if dst_names else 0

    return ParseResult(
        max_sources=max_src,
        max_destinations=max_dst,
        source_names=src_names,
        destination_names=dst_names,
        format_detected="MAGNUM profile_availability",
        hidden_sources=hidden_src,
        hidden_destinations=hidden_dst,
        warnings=warnings,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Format 2: Generic alias export  (Type,Number,Name rows)
# ─────────────────────────────────────────────────────────────────────────────

def _parse_alias_export(text: str) -> ParseResult | None:
    src_names: dict[int, str] = {}
    dst_names: dict[int, str] = {}
    warnings: list[str] = []
    matched = 0

    reader = csv.reader(io.StringIO(text))
    for row in reader:
        if not row:
            continue
        row = [c.strip() for c in row]
        kind = row[0].upper()
        if kind in ("TYPE", "SOURCE", "DESTINATION", "#", ""):
            continue
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

    return ParseResult(
        max_sources=max(src_names.keys()) if src_names else 0,
        max_destinations=max(dst_names.keys()) if dst_names else 0,
        source_names=src_names,
        destination_names=dst_names,
        format_detected="alias export",
        warnings=warnings,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Format 3: Two-column CSV  (Source,Destination header)
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
        format_detected="two-column CSV",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Format 4: Shorthand  (32,32 / 32/16 / 64x128 / sources=32 destinations=16)
# ─────────────────────────────────────────────────────────────────────────────

def _parse_shorthand(text: str) -> ParseResult | None:
    src_m = re.search(r"(?:sources?|src|inputs?)\s*[=:]\s*(\d+)", text, re.I)
    dst_m = re.search(r"(?:destinations?|dst|dest|outputs?)\s*[=:]\s*(\d+)", text, re.I)
    if src_m and dst_m:
        return ParseResult(
            max_sources=int(src_m.group(1)),
            max_destinations=int(dst_m.group(1)),
            source_names={}, destination_names={},
            format_detected="shorthand key=value",
        )

    m = re.fullmatch(r"\s*(\d+)\s*[,/ x×]\s*(\d+)\s*", text, re.I)
    if m:
        return ParseResult(
            max_sources=int(m.group(1)),
            max_destinations=int(m.group(2)),
            source_names={}, destination_names={},
            format_detected="shorthand NxN",
        )

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Format 5: Sectioned list  (Sources / Destinations section headers)
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
        format_detected="sectioned list",
    )
