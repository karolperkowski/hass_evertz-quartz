"""Shared fixtures for the Evertz Quartz test suite."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow tests to load the custom integration from custom_components/."""
    return
