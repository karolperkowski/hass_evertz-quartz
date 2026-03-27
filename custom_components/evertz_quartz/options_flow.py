"""Options flow for Evertz Quartz — settings + CSV re-import with diff summary."""

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
    CONF_CLIENT_VERBOSE,
    CONF_CONNECT_TIMEOUT,
    CONF_CSV_LOADED,
    CONF_LEVELS,
    CONF_MAX_DESTINATIONS,
    CONF_MAX_SOURCES,
    CONF_NAME,
    CONF_RECONNECT_DELAY,
    CONF_VERBOSE_LOGGING,
    DEFAULT_CLIENT_VERBOSE,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_LEVELS,
    DEFAULT_MAX_DESTINATIONS,
    DEFAULT_MAX_SOURCES,
    DEFAULT_RECONNECT_DELAY,
    DEFAULT_VERBOSE_LOGGING,
    DOMAIN,
)
from .csv_parser import ParseResult, parse_csv

_LOGGER = logging.getLogger(__name__)
_MAX_SIZE = 2048
CONF_CSV_UPLOAD = "csv_upload"


def _effective(entry: ConfigEntry, key: str, default):
    """Options override data, data overrides default."""
    if key in entry.options:
        return entry.options[key]
    return entry.data.get(key, default)


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


def _build_diff_summary(
    entry: ConfigEntry,
    result: ParseResult,
) -> dict:
    """
    Compare parsed CSV against current effective config.
    Returns a dict with 'changes', 'reload_needed', 'warnings'.
    """
    cur_src = _effective(entry, CONF_MAX_SOURCES,     DEFAULT_MAX_SOURCES)
    cur_dst = _effective(entry, CONF_MAX_DESTINATIONS, DEFAULT_MAX_DESTINATIONS)
    cur_src_names = entry.data.get("source_names", {})
    cur_dst_names = entry.data.get("destination_names", {})

    changes: list[str] = []
    reload_needed = False

    # Source count
    if result.max_sources > 0 and result.max_sources != cur_src:
        changes.append(f"Max Sources: {cur_src} → {result.max_sources}")
    # Destination count
    if result.max_destinations > 0 and result.max_destinations != cur_dst:
        changes.append(f"Max Destinations: {cur_dst} → {result.max_destinations}")
        reload_needed = True

    # Names — always show as a change when CSV provides them,
    # even if the count is the same (names may have been updated on the router)
    new_src_names = len(result.source_names)
    new_dst_names = len(result.destination_names)
    old_src_names = len(cur_src_names)

    if new_src_names > 0:
        changes.append(
            f"Source names: {old_src_names} → {new_src_names} "
            f"(max Order {result.max_sources})"
        )
    if new_dst_names > 0:
        changes.append(
            f"Destination names: {len(cur_dst_names)} → {new_dst_names} "
            f"(max Order {result.max_destinations})"
        )

    if result.hidden_sources or result.hidden_destinations:
        changes.append(
            f"Hidden ports: {result.hidden_sources} src, "
            f"{result.hidden_destinations} dst (excluded from profile)"
        )

    warnings = list(result.warnings)

    return {
        "changes": changes,
        "reload_needed": reload_needed,
        "warnings": warnings,
        "result": result,
    }


class EvertzQuartzOptionsFlow(OptionsFlow):
    """Configure panel — settings tab + CSV re-import with diff summary."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        self._entry = config_entry
        self._pending_diff: dict | None = None   # set when CSV is parsed, awaiting confirm

    # ── Main settings form ────────────────────────────────────────────────

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        """Main options form: sizes, levels, debug settings, optional CSV upload."""
        errors: dict[str, str] = {}

        if user_input is not None:
            upload_id = user_input.pop(CONF_CSV_UPLOAD, None)

            if upload_id:
                # Parse CSV on executor thread (file I/O)
                result, err_msg = await self.hass.async_add_executor_job(
                    _parse_uploaded_csv, self.hass, upload_id
                )
                if result is None:
                    errors[CONF_CSV_UPLOAD] = "csv_parse_error"
                    self._pending_diff = None
                else:
                    diff = _build_diff_summary(self._entry, result)
                    if diff["changes"]:
                        # Store pending diff + current user_input, go to confirm
                        self._pending_diff = {**diff, "settings": user_input}
                        return await self.async_step_confirm_csv()
                    else:
                        _LOGGER.info("CSV uploaded but no changes detected — skipping confirmation")

            if not errors:
                return await self._apply_settings(user_input)

        return self._show_init_form(errors)

    def _show_init_form(self, errors: dict) -> FlowResult:
        cur_max_src     = _effective(self._entry, CONF_MAX_SOURCES,      DEFAULT_MAX_SOURCES)
        cur_max_dst     = _effective(self._entry, CONF_MAX_DESTINATIONS,  DEFAULT_MAX_DESTINATIONS)
        cur_levels      = _effective(self._entry, CONF_LEVELS,            DEFAULT_LEVELS)
        cur_verbose     = _effective(self._entry, CONF_VERBOSE_LOGGING,   DEFAULT_VERBOSE_LOGGING)
        cur_cli_verbose = _effective(self._entry, CONF_CLIENT_VERBOSE,    DEFAULT_CLIENT_VERBOSE)
        cur_recon       = _effective(self._entry, CONF_RECONNECT_DELAY,   DEFAULT_RECONNECT_DELAY)
        cur_timeout     = _effective(self._entry, CONF_CONNECT_TIMEOUT,   DEFAULT_CONNECT_TIMEOUT)
        router_name     = self._entry.data.get(CONF_NAME) or self._entry.data.get("host", "")
        csv_status      = "CSV profile loaded" if self._entry.data.get(CONF_CSV_LOADED) else "No CSV — using router names"

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(CONF_MAX_SOURCES,      default=cur_max_src):      vol.All(int, vol.Range(min=1, max=_MAX_SIZE)),
                vol.Required(CONF_MAX_DESTINATIONS, default=cur_max_dst):      vol.All(int, vol.Range(min=1, max=_MAX_SIZE)),
                vol.Required(CONF_LEVELS,           default=cur_levels):       str,
                vol.Required(CONF_VERBOSE_LOGGING,  default=cur_verbose):      bool,
                vol.Required(CONF_RECONNECT_DELAY,  default=cur_recon):        vol.All(int, vol.Range(min=1, max=300)),
                vol.Required(CONF_CONNECT_TIMEOUT,  default=cur_timeout):      vol.All(int, vol.Range(min=3, max=60)),
                vol.Optional(CONF_CSV_UPLOAD): FileSelector(
                    FileSelectorConfig(accept=".csv,text/csv")
                ),
            }),
            errors=errors,
            description_placeholders={
                "router_name":  router_name,
                "current_size": f"{cur_max_src} src x {cur_max_dst} dst",
                "csv_status":   csv_status,
            },
        )

    # ── CSV diff / confirm step ───────────────────────────────────────────

    async def async_step_confirm_csv(self, user_input: dict | None = None) -> FlowResult:
        """Show a read-only diff of what the CSV will change. User confirms or cancels."""
        if user_input is not None:
            if user_input.get("confirmed"):
                # User confirmed — apply settings + CSV overrides
                diff = self._pending_diff
                settings = diff["settings"]
                result: ParseResult = diff["result"]

                # Merge CSV counts into settings
                if result.max_sources > 0:
                    settings[CONF_MAX_SOURCES] = result.max_sources
                if result.max_destinations > 0:
                    settings[CONF_MAX_DESTINATIONS] = result.max_destinations

                self._pending_diff = None
                return await self._apply_settings(
                    settings,
                    source_names=result.source_names if result.max_sources > 0 else None,
                    destination_names=result.destination_names if result.max_destinations > 0 else None,
                    source_port_map=result.source_port_map if result.max_sources > 0 else None,
                    destination_port_map=result.destination_port_map if result.max_destinations > 0 else None,
                )
            else:
                # Cancelled — go back to init form
                self._pending_diff = None
                return self._show_init_form({})

        diff = self._pending_diff
        changes = diff["changes"]
        reload_needed = diff["reload_needed"]
        warnings = diff["warnings"]

        # Build a human-readable summary for the description placeholder
        lines = ["**What will change:**", ""]
        for change in changes:
            lines.append(f"• {change}")
        if reload_needed:
            lines.append("")
            lines.append("⚠ Max Destinations changed — the integration will **reload** to create or remove entities.")
        if warnings:
            lines.append("")
            lines.append("**Warnings:**")
            for w in warnings:
                lines.append(f"• {w}")

        summary = "\n".join(lines)

        return self.async_show_form(
            step_id="confirm_csv",
            data_schema=vol.Schema({
                vol.Required("confirmed", default=True): bool,
            }),
            description_placeholders={"summary": summary},
        )

    # ── Apply helper ──────────────────────────────────────────────────────

    async def _apply_settings(
        self,
        settings: dict,
        source_names: dict[int, str] | None = None,
        destination_names: dict[int, str] | None = None,
        source_port_map: dict[int, int] | None = None,
        destination_port_map: dict[int, int] | None = None,
    ) -> FlowResult:
        """
        Apply validated settings to the running client and save options.
        source_names / destination_names / port_maps are optional CSV-sourced overrides.
        """
        client = (
            self.hass.data.get(DOMAIN, {})
            .get(self._entry.entry_id, {})
            .get("client")
        )

        old_max_src = _effective(self._entry, CONF_MAX_SOURCES,     DEFAULT_MAX_SOURCES)
        old_max_dst = _effective(self._entry, CONF_MAX_DESTINATIONS, DEFAULT_MAX_DESTINATIONS)
        old_levels  = _effective(self._entry, CONF_LEVELS,           DEFAULT_LEVELS)

        new_max_src = settings[CONF_MAX_SOURCES]
        new_max_dst = settings[CONF_MAX_DESTINATIONS]
        new_levels  = settings[CONF_LEVELS]

        # ── Apply debug/connection options live ───────────────────────────
        if client:
            client.update_options(
                verbose_logging=settings.get(CONF_VERBOSE_LOGGING),
                reconnect_delay=settings.get(CONF_RECONNECT_DELAY),
                connect_timeout=settings.get(CONF_CONNECT_TIMEOUT),
            )

        # ── Apply levels live ─────────────────────────────────────────────
        if new_levels != old_levels and client:
            client.levels = new_levels
            _LOGGER.info("Levels updated live: %s", new_levels)
            self.hass.async_create_task(client.query_all_mnemonics())

        # ── Apply max_sources live ────────────────────────────────────────
        if new_max_src != old_max_src and client:
            client.max_sources = new_max_src
            _LOGGER.info("Max sources updated live: %d", new_max_src)
            self.hass.async_create_task(client.query_all_routes())

        # ── Push CSV source names live ────────────────────────────────────
        if source_names and client:
            client.source_names.update(source_names)
            _LOGGER.info("Loaded %d source names from CSV", len(source_names))

        # ── Push CSV destination names live (if dst count unchanged) ──────
        if destination_names and client and new_max_dst == old_max_dst:
            client.destination_names.update(destination_names)
            _LOGGER.info("Loaded %d destination names from CSV", len(destination_names))

        # Trigger entity redraw for any name changes
        if (source_names or destination_names) and client:
            for cb in self.hass.data.get(DOMAIN, {}).get(
                self._entry.entry_id, {}
            ).get("mnemonic_listeners", []):
                self.hass.loop.call_soon_threadsafe(cb)

        # ── Persist names into entry.data via hass.config_entries ────────
        # Names go into data (not options) so they survive options resets
        # Persist port maps + names when CSV provided; update csv_loaded flag
        if source_port_map or destination_port_map or source_names or destination_names:
            new_data = dict(self._entry.data)
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

        result = self.async_create_entry(title="", data=settings)

        # ── Reload if destination count changed ───────────────────────────
        if new_max_dst != old_max_dst:
            _LOGGER.info(
                "Max destinations changed %d → %d — reloading integration",
                old_max_dst, new_max_dst,
            )
            self.hass.async_create_task(
                self.hass.config_entries.async_reload(self._entry.entry_id)
            )

        return result
