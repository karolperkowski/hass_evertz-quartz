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


async def test_reconfigure_updates_host_and_reloads(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="old.local:6666",
        title="MY-ROUTER",
        data={
            "host": "old.local",
            "port": 6666,
            "router_name": "MY-ROUTER",
            "max_sources": 32,
            "csv_loaded": True,
            "source_names": {"1": "SRC-001"},
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    with (
        patch(
            "custom_components.evertz_quartz.config_flow._validate_connection",
            return_value=None,
        ),
        # The abort triggers an entry reload — keep the real setup (TCP
        # connect, notifications) out of the test.
        patch(
            "custom_components.evertz_quartz.async_setup_entry", return_value=True
        ),
        patch(
            "custom_components.evertz_quartz.async_unload_entry", return_value=True
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {"host": "new.local", "port": 7777, "router_name": ""},
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["host"] == "new.local"
    assert entry.data["port"] == 7777
    assert entry.unique_id == "new.local:7777"
    # Profile/CSV data preserved
    assert entry.data["csv_loaded"] is True
    assert entry.data["source_names"] == {"1": "SRC-001"}
    # Name kept when left blank
    assert entry.data["router_name"] == "MY-ROUTER"


async def test_reconfigure_aborts_on_collision_with_other_entry(hass) -> None:
    MockConfigEntry(
        domain=DOMAIN,
        unique_id="other.local:6666",
        data={"host": "other.local", "port": 6666},
    ).add_to_hass(hass)
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="mine.local:6666",
        data={"host": "mine.local", "port": 6666},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"host": "other.local", "port": 6666, "router_name": ""},
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
