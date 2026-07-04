"""Tests for the config flow — happy path and duplicate-router abort."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.evertz_quartz.const import DOMAIN


async def test_full_flow_manual_counts(hass) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    with patch(
        "custom_components.evertz_quartz.config_flow._validate_connection",
        return_value=None,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"host": "router.local", "port": 6666, "router_name": "MY-ROUTER"},
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "profile"

    with patch(
        "custom_components.evertz_quartz.async_setup_entry", return_value=True
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"max_sources": 64, "max_destinations": 16, "levels": "V"},
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "MY-ROUTER"
    data = result["data"]
    assert data["host"] == "router.local"
    assert data["port"] == 6666
    assert data["max_sources"] == 64
    assert data["max_destinations"] == 16
    assert data["csv_loaded"] is False
    assert result["result"].unique_id == "router.local:6666"


async def test_duplicate_router_aborts(hass) -> None:
    MockConfigEntry(
        domain=DOMAIN,
        unique_id="router.local:6666",
        data={"host": "router.local", "port": 6666},
    ).add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"host": "router.local", "port": 6666, "router_name": ""},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_cannot_connect_shows_error(hass) -> None:
    from custom_components.evertz_quartz.config_flow import CannotConnect

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(
        "custom_components.evertz_quartz.config_flow._validate_connection",
        side_effect=CannotConnect,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"host": "router.local", "port": 6666, "router_name": ""},
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}
