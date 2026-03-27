"""Options flow for Evertz Quartz — settings + profile management."""

from __future__ import annotations

import logging
from pathlib import Path

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, OptionsFlow
from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.selector import (
    FileSelector,
    FileSelectorConfig,
)

from .const import (
    CONF_CONNECT_TIMEOUT,
    CONF_CSV_LOADED,
    CONF_LEVELS,
    CONF_MAX_DESTINATIONS,
    CONF_MAX_SOURCES,
    CONF_NAME,
    CONF_RECONNECT_DELAY,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_LEVELS,
    DEFAULT_MAX_DESTINATIONS,
    DEFAULT_MAX_SOURCES,
    DEFAULT_RECONNECT_DELAY,
    DOMAIN,
)
from .helpers import effective, router_display_name
from .csv_parser import ParseResult, parse_csv

_LOGGER = logging.getLogger(__name__)
_MAX_SIZE = 2048
CONF_CSV_UPLOAD = "csv_upload"


def _parse_uploaded_csv(hass, upload_id: str) -> tuple[ParseResult | None, str]:
    """Read uploaded file and parse it. Returns (result, error_message)."""
    try:
        with process_uploaded_file(hass, upload_id) as file_path:
            text = Path(file_path).read_text(encoding="utf-8", errors="replace")
    except Exception as err:  # noqa: BLE001
        return None, f"Could not read file: {err}"
    result = parse_csv(text)
    if result is None:
        return None, "File could not be parsed — check format and try again."
    return result, ""


def _build_csv_diff(entry: ConfigEntry, result: ParseResult) -> dict:
    """
    Compare parsed CSV against current config.
    Returns dict with 'changes', 'warnings', 'result'.
    Any CSV import always triggers a full reload.
    """
    cur_src = effective(entry, CONF_MAX_SOURCES,      DEFAULT_MAX_SOURCES)
    cur_dst = effective(entry, CONF_MAX_DESTINATIONS, DEFAULT_MAX_DESTINATIONS)
    cur_src_names = entry.data.get("source_names", {})
    cur_dst_names = entry.data.get("destination_names", {})

    changes: list[str] = []

    if result.max_sources > 0 and result.max_sources != cur_src:
        changes.append(f"Max Sources: {cur_src} \u2192 {result.max_sources}")
    if result.max_destinations > 0 and result.max_destinations != cur_dst:
        changes.append(f"Max Destinations: {cur_dst} \u2192 {result.max_destinations}")

    new_src = len(result.source_names)
    new_dst = len(result.destination_names)
    if new_src > 0:
        changes.append(
            f"Source names: {len(cur_src_names)} \u2192 {new_src} (max Order {result.max_sources})"
        )
    if new_dst > 0:
        changes.append(
            f"Destination names: {len(cur_dst_names)} \u2192 {new_dst} (max Order {result.max_destinations})"
        )
    if result.hidden_sources or result.hidden_destinations:
        changes.append(
            f"Hidden ports: {result.hidden_sources} src, "
            f"{result.hidden_destinations} dst (excluded from profile)"
        )

    warnings = list(result.warnings)
    if result.has_port_gaps:
        warnings.append(
            "Non-contiguous port numbering detected \u2014 routing uses Order numbers correctly."
        )

    return {"changes": changes, "warnings": warnings, "result": result}


def _build_count_diff(entry: ConfigEntry, new_src: int, new_dst: int) -> dict:
    """
    Compare manually entered counts against current config.
    Returns dict with 'changes' and 'notes'.
    """
    cur_src = effective(entry, CONF_MAX_SOURCES,      DEFAULT_MAX_SOURCES)
    cur_dst = effective(entry, CONF_MAX_DESTINATIONS, DEFAULT_MAX_DESTINATIONS)
    csv_loaded = entry.data.get(CONF_CSV_LOADED, False)

    changes: list[str] = []
    notes: list[str] = []

    if new_src != cur_src:
        changes.append(f"Max Sources: {cur_src} \u2192 {new_src}")
        if new_src > cur_src:
            notes.append(
                f"New source slots ({cur_src + 1}\u2013{new_src}) will show as "
                f'"Source N" until a CSV is imported.'
            )
        else:
            notes.append(
                f"Sources {new_src + 1}\u2013{cur_src} will be removed from the options list."
            )

    if new_dst != cur_dst:
        changes.append(f"Max Destinations: {cur_dst} \u2192 {new_dst}")
        if new_dst > cur_dst:
            notes.append(
                f"New destination entities will be created for destinations "
                f"{cur_dst + 1}\u2013{new_dst}."
            )
        else:
            notes.append(
                f"Destination entities {new_dst + 1}\u2013{cur_dst} will be removed."
            )

    if csv_loaded and changes:
        notes.append(
            "Existing CSV names are preserved for slots within the new range."
        )

    return {"changes": changes, "notes": notes}


class EvertzQuartzOptionsFlow(OptionsFlow):
    """
    Configure panel — two steps:

    Step 1 (init)     — Connection settings: levels, reconnect delay, timeout.
    Step 2 (profile)  — Router profile: CSV upload or manual count adjustment.
    Step 3 (confirm)  — Diff summary and confirmation before any reload.

    Counts are always written to entry.data (not just entry.options) so they
    survive if options are ever reset. Any count or CSV change triggers a reload.
    """

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry
        self._pending: dict | None = None
        self._saved_settings: dict | None = None

    # ── Step 1: Connection settings ───────────────────────────────────────

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        """
        Connection settings — levels, reconnect delay, connection timeout.
        Saving this step moves to the Profile step.
        """
        if user_input is not None:
            self._saved_settings = user_input
            return await self.async_step_profile()

        cur_levels  = effective(self._entry, CONF_LEVELS,           DEFAULT_LEVELS)
        cur_recon   = effective(self._entry, CONF_RECONNECT_DELAY,  DEFAULT_RECONNECT_DELAY)
        cur_timeout = effective(self._entry, CONF_CONNECT_TIMEOUT,  DEFAULT_CONNECT_TIMEOUT)
        rname       = router_display_name(self._entry)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(CONF_LEVELS,          default=cur_levels):   str,
                vol.Required(CONF_RECONNECT_DELAY, default=cur_recon):    vol.All(int, vol.Range(min=1, max=300)),
                vol.Required(CONF_CONNECT_TIMEOUT, default=cur_timeout):  vol.All(int, vol.Range(min=3, max=60)),
            }),
            description_placeholders={"router_name": rname},
            last_step=False,
        )

    # ── Step 2: Profile ───────────────────────────────────────────────────

    async def async_step_profile(self, user_input: dict | None = None) -> FlowResult:
        """
        Router profile — upload a new CSV or adjust counts manually.

        CSV upload takes priority over manually entered counts.
        Any change here triggers a confirm step before applying.
        """
        errors: dict[str, str] = {}

        if user_input is not None:
            upload_id = user_input.pop(CONF_CSV_UPLOAD, None)
            new_src   = user_input.get(CONF_MAX_SOURCES)
            new_dst   = user_input.get(CONF_MAX_DESTINATIONS)

            if upload_id:
                result, err_msg = await self.hass.async_add_executor_job(
                    _parse_uploaded_csv, self.hass, upload_id
                )
                if result is None:
                    errors[CONF_CSV_UPLOAD] = "csv_parse_error"
                else:
                    diff = _build_csv_diff(self._entry, result)
                    self._pending = {
                        "kind": "csv",
                        "diff": diff,
                        "new_src": result.max_sources if result.max_sources > 0 else new_src,
                        "new_dst": result.max_destinations if result.max_destinations > 0 else new_dst,
                    }
                    return await self.async_step_confirm()

            if not errors:
                cur_src = effective(self._entry, CONF_MAX_SOURCES,      DEFAULT_MAX_SOURCES)
                cur_dst = effective(self._entry, CONF_MAX_DESTINATIONS, DEFAULT_MAX_DESTINATIONS)
                final_src = new_src if new_src is not None else cur_src
                final_dst = new_dst if new_dst is not None else cur_dst
                diff = _build_count_diff(self._entry, final_src, final_dst)

                if diff["changes"]:
                    self._pending = {
                        "kind": "manual",
                        "diff": diff,
                        "new_src": final_src,
                        "new_dst": final_dst,
                    }
                    return await self.async_step_confirm()

                # No profile changes — apply connection settings only
                return await self._apply()

        cur_src    = effective(self._entry, CONF_MAX_SOURCES,      DEFAULT_MAX_SOURCES)
        cur_dst    = effective(self._entry, CONF_MAX_DESTINATIONS, DEFAULT_MAX_DESTINATIONS)
        rname      = router_display_name(self._entry)
        csv_loaded = self._entry.data.get(CONF_CSV_LOADED, False)
        csv_status = (
            f"CSV loaded \u2014 "
            f"{len(self._entry.data.get('source_names', {}))} source names, "
            f"{len(self._entry.data.get('destination_names', {}))} destination names"
            if csv_loaded
            else "No CSV loaded \u2014 entities show generic names (Source N / Destination N)"
        )

        return self.async_show_form(
            step_id="profile",
            data_schema=vol.Schema({
                vol.Required(CONF_MAX_SOURCES,      default=cur_src):  vol.All(int, vol.Range(min=1, max=_MAX_SIZE)),
                vol.Required(CONF_MAX_DESTINATIONS, default=cur_dst):  vol.All(int, vol.Range(min=1, max=_MAX_SIZE)),
                vol.Optional(CONF_CSV_UPLOAD): FileSelector(
                    FileSelectorConfig(accept=".csv,text/csv")
                ),
            }),
            errors=errors,
            description_placeholders={
                "router_name":  rname,
                "current_size": f"{cur_src} sources \u00d7 {cur_dst} destinations",
                "csv_status":   csv_status,
            },
        )

    # ── Step 3: Confirm ───────────────────────────────────────────────────

    async def async_step_confirm(self, user_input: dict | None = None) -> FlowResult:
        """
        Confirm step — shown before any reload-triggering change.
        Displays a diff summary and asks the user to confirm or cancel.
        Cancelling returns to the Profile step.
        """
        if user_input is not None:
            if user_input.get("confirmed"):
                p = self._pending
                self._pending = None
                if p["kind"] == "csv":
                    result: ParseResult = p["diff"]["result"]
                    return await self._apply(
                        new_src=p["new_src"],
                        new_dst=p["new_dst"],
                        source_names=result.source_names if result.max_sources > 0 else None,
                        destination_names=result.destination_names if result.max_destinations > 0 else None,
                        source_port_map=result.source_port_map if result.max_sources > 0 else None,
                        destination_port_map=result.destination_port_map if result.max_destinations > 0 else None,
                    )
                else:
                    return await self._apply(
                        new_src=p["new_src"],
                        new_dst=p["new_dst"],
                    )
            else:
                self._pending = None
                return await self.async_step_profile()

        p = self._pending
        lines = ["**What will change:**", ""]

        if p["kind"] == "csv":
            diff = p["diff"]
            for c in diff["changes"]:
                lines.append(f"\u2022 {c}")
            lines += ["", "\u26a0 The integration will **reload** to apply all changes."]
            if diff["warnings"]:
                lines += ["", "**Warnings:**"]
                for w in diff["warnings"]:
                    lines.append(f"\u2022 {w}")
        else:
            diff = p["diff"]
            for c in diff["changes"]:
                lines.append(f"\u2022 {c}")
            for n in diff["notes"]:
                lines.append(f"\u2139 {n}")
            lines += ["", "\u26a0 The integration will **reload** to apply these changes."]

        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({
                vol.Required("confirmed", default=True): bool,
            }),
            description_placeholders={"summary": "\n".join(lines)},
        )

    # ── Apply ─────────────────────────────────────────────────────────────

    async def _apply(
        self,
        new_src: int | None = None,
        new_dst: int | None = None,
        source_names: dict[int, str] | None = None,
        destination_names: dict[int, str] | None = None,
        source_port_map: dict[int, int] | None = None,
        destination_port_map: dict[int, int] | None = None,
    ) -> FlowResult:
        """
        Persist all changes and optionally reload.

        Counts always go to entry.data (not just entry.options) so they
        survive options resets. A reload is triggered when counts or CSV change.
        """
        settings = self._saved_settings or {}
        client = (
            self.hass.data.get(DOMAIN, {})
            .get(self._entry.entry_id, {})
            .get("client")
        )

        old_src = effective(self._entry, CONF_MAX_SOURCES,      DEFAULT_MAX_SOURCES)
        old_dst = effective(self._entry, CONF_MAX_DESTINATIONS, DEFAULT_MAX_DESTINATIONS)
        old_lvl = effective(self._entry, CONF_LEVELS,            DEFAULT_LEVELS)

        final_src = new_src if new_src is not None else old_src
        final_dst = new_dst if new_dst is not None else old_dst
        final_lvl = settings.get(CONF_LEVELS, old_lvl)

        csv_changed = bool(
            source_names or destination_names or source_port_map or destination_port_map
        )

        # ── Apply connection options live ─────────────────────────────────
        if client:
            client.update_options(
                reconnect_delay=settings.get(CONF_RECONNECT_DELAY),
                connect_timeout=settings.get(CONF_CONNECT_TIMEOUT),
            )

        # ── Apply levels live (only when no CSV/count change) ─────────────
        if final_lvl != old_lvl and client and not csv_changed:
            client.levels = final_lvl
            _LOGGER.info("Levels updated live: %s", final_lvl)

        # ── Write counts + CSV data to entry.data ─────────────────────────
        new_data = dict(self._entry.data)
        new_data[CONF_MAX_SOURCES]      = final_src
        new_data[CONF_MAX_DESTINATIONS] = final_dst

        if csv_changed:
            if source_port_map:
                new_data["source_port_map"] = {str(k): v for k, v in source_port_map.items()}
            if destination_port_map:
                new_data["destination_port_map"] = {str(k): v for k, v in destination_port_map.items()}
            if source_names:
                new_data["source_names"] = {str(k): v for k, v in source_names.items()}
            if destination_names:
                new_data["destination_names"] = {str(k): v for k, v in destination_names.items()}
            new_data[CONF_CSV_LOADED] = True

        self.hass.config_entries.async_update_entry(self._entry, data=new_data)

        # ── Build options entry (connection settings only) ────────────────
        options_data: dict = {CONF_LEVELS: final_lvl}
        if CONF_RECONNECT_DELAY in settings:
            options_data[CONF_RECONNECT_DELAY] = settings[CONF_RECONNECT_DELAY]
        if CONF_CONNECT_TIMEOUT in settings:
            options_data[CONF_CONNECT_TIMEOUT] = settings[CONF_CONNECT_TIMEOUT]
        # Preserve log level selections
        for key in ("client_log_level", "integration_log_level"):
            if key in self._entry.options:
                options_data[key] = self._entry.options[key]

        result = self.async_create_entry(title="", data=options_data)

        # ── Reload when counts or CSV changed ─────────────────────────────
        needs_reload = csv_changed or final_src != old_src or final_dst != old_dst
        if needs_reload:
            _LOGGER.info(
                "Profile change \u2014 reloading (src: %d\u2192%d, dst: %d\u2192%d, csv: %s)",
                old_src, final_src, old_dst, final_dst, csv_changed,
            )
            self.hass.async_create_task(
                self.hass.config_entries.async_reload(self._entry.entry_id)
            )

        return result
