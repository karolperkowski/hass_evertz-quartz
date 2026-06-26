#!/usr/bin/env python3
"""Fail unless manifest.json "version" is greater than the base branch's.

Enforces the CLAUDE.md rule "Bump the version on every commit". Used by
.github/workflows/version-bump.yml on pull requests, and runnable locally:

    python3 .github/scripts/check_version_bump.py main
"""
from __future__ import annotations

import json
import subprocess
import sys

MANIFEST = "custom_components/evertz_quartz/manifest.json"


def parse(version: str) -> tuple[int, ...]:
    """Parse 'x.y.z' into a comparable tuple, ignoring non-numeric noise."""
    parts: list[int] = []
    for piece in version.split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def head_version() -> str:
    with open(MANIFEST, encoding="utf-8") as fh:
        return json.load(fh)["version"]


def base_version(base_ref: str) -> str | None:
    """Version of MANIFEST on the base branch, or None if it didn't exist."""
    for ref in (f"origin/{base_ref}", base_ref, "FETCH_HEAD"):
        try:
            out = subprocess.check_output(
                ["git", "show", f"{ref}:{MANIFEST}"],
                text=True, stderr=subprocess.DEVNULL,
            )
            return json.loads(out)["version"]
        except subprocess.CalledProcessError:
            continue
    return None


def main() -> int:
    base_ref = sys.argv[1] if len(sys.argv) > 1 else "main"
    head = head_version()
    base = base_version(base_ref)
    print(f"base ({base_ref}) version: {base}")
    print(f"PR head version:         {head}")

    if base is None:
        print("No manifest on base branch — nothing to compare (treated as OK).")
        return 0
    if parse(head) > parse(base):
        print(f"OK: version bumped {base} -> {head}")
        return 0

    print(
        f"::error::manifest.json version must be bumped above {base} "
        f"(found {head}). See CLAUDE.md → Rules: bump the version on every commit."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
