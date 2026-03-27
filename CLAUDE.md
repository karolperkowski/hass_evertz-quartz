# CR47 — Evertz Quartz Router Home Assistant Integration

## Session Summary

This session built a complete Home Assistant custom integration for controlling an **Evertz EQX video router** via a **MAGNUM controller** using the Quartz Remote Control Protocol, published to GitHub as a HACS-compatible integration.

---

## Repository

**https://github.com/karolperkowski/hass_evertz-quartz**

HACS custom repository — Category: Integration — HA minimum: 2024.6.0

---

## What Was Built

### Integration (`custom_components/evertz_quartz/`)

| File | Purpose |
|---|---|
| `__init__.py` | Entry setup, multi-router service, CSV name loading |
| `quartz_client.py` | Asyncio TCP client for Quartz protocol |
| `config_flow.py` | 2-step setup UI — connect + CSV profile upload |
| `options_flow.py` | Configure panel — resize, re-import CSV, debug settings |
| `select.py` | Destination select entities + dual log level controls |
| `button.py` | Resync buttons + Clear CSV Profile button |
| `diagnostics.py` | HA diagnostics download support |
| `csv_parser.py` | MAGNUM `profile_availability.csv` parser |
| `services.yaml` | HA service schema for `evertz_quartz.route` |
| `const.py` | All constants and defaults |

---

## Key Technical Discoveries

### MAGNUM Quartz Protocol Behaviour
- **Port 6666** — MAGNUM's Quartz TCP interface
- **Order numbers not Port numbers** — MAGNUM communicates entirely using the sequential `Order` column from `profile_availability.csv`, NOT the `Port Number` (Quartz crosspoint address). Port numbers are handled internally by MAGNUM.
- **CR-only line endings** — Quartz terminates messages with `\r` (0x0D) only, not `\n`. Must use `readuntil(b'\r')` not `readline()`.
- **No `.RT`/`.RD` responses** — MAGNUM ignores mnemonic queries. Names come from CSV only.
- **No `.QL` response** — `.QL` is not a valid Quartz command. Use `.I{level}{dest}` (Interrogate Route).
- **`.UV` updates work** — MAGNUM does send unsolicited route updates: `.UV1,360` means destination Order=1 routed to source Order=360.
- **Optimistic routing** — `client.routes` updated immediately on `.SV` send since MAGNUM may not echo back `.UV` for every take.

### CSV Profile Format
```
Device Short Name, Src or Dst, Port Number, Global Name, Hidden?, Order
VP, SRC, 1, 57CAM1, 0, 1
VP, DST, 323, QC4720, 0, 1
```
- **`Order`** = entity count / routing number (what MAGNUM uses)
- **`Port Number`** = physical Quartz crosspoint (stored for diagnostics only)
- 438 sources have `Order ≠ Port` (tieline/remote sources use non-contiguous port numbers)
- Profile tested: 1164 sources, 1 destination (`QC4720`)

---

## Architecture

### Names
- CSV names stored in `entry.data` when uploaded (`csv_loaded=True`)
- Loaded into client memory on every HA startup
- If no CSV: `.RT`/`.RD` queries sent on connect (non-MAGNUM routers only)
- Names keyed by **Order**, not Port Number

### Routing
- `.SV{level}{dest_order},{src_order}\r` sent to route
- Optimistic state updated immediately in `client.routes`
- `.UV{level}{dest_order},{src_order}` received for external changes
- `.I{level}{dest_order}` sent on connect to query current state

### Multi-router Service
```yaml
service: evertz_quartz.route
data:
  device_id: "abc123"      # HA device registry ID
  # OR
  router_name: "CR47"      # matches CONF_NAME or IP
  destination: 1           # Order index
  source: 360              # Order index
  levels: "V"              # optional
```

### Port Maps
`source_port_map` and `destination_port_map` (`{order: quartz_port}`) stored in `entry.data` for diagnostics reference. Not used in routing commands.

---

## Entities (per router)

| Entity | Type | Category | Description |
|---|---|---|---|
| `select.{name}_qc4720` | Select | — | One per destination — routes sources |
| `select.{name}_log_level` | Select | Diagnostic | Integration log level (persists across restarts) |
| `select.{name}_client_log_level` | Select | Diagnostic | quartz_client log level (persists) |
| `button.{name}_resync_all` | Button | Diagnostic | Re-polls names + routes |
| `button.{name}_resync_routes` | Button | Diagnostic | Re-polls routes only |
| `button.{name}_resync_names` | Button | Diagnostic | Re-polls names only |
| `button.{name}_clear_csv` | Button | Diagnostic | Clears CSV profile, reverts to router names |

---

## Dashboard (`cr47_dashboard_v2.yaml`)

Native HA cards only — no HACS frontend dependencies.

- **Active Source** — tile card showing current QC4720 route, updates in real-time
- **Cameras** — 57CAM1–8 button grid
- **Production** — Program, Preview, VIZ1, VIZ2, CLIP1, CLIP2, DDR7, BARS
- **Macros** — direct `perform-action: select.select_option` buttons (no scripts)
- **Route Log** — logbook card, 24-hour history of QC4720 changes
- **Diagnostics** — resync buttons, log level selects

---

## Known Limitations / Future Work

- MAGNUM does not respond to `.I` interrogate on connect — initial route state is unknown until MAGNUM sends a `.UV` or user makes a route change
- 1164 sources in a dropdown is unwieldy — a custom Lovelace matrix card would improve UX significantly
- No salvo support (MAGNUM profile has none configured)
- HomeKit Bridge will try to expose all 1164+ entities — exclude `evertz_quartz` domain in HomeKit Bridge filter to avoid IID collisions
