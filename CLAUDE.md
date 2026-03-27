# hass_evertz-quartz — Session Reference

## Repository
**https://github.com/karolperkowski/hass_evertz-quartz**

HACS custom integration — Category: Integration — HA minimum: 2024.6.0

The Lovelace card lives in a separate repo:
**https://github.com/karolperkowski/lovelace-evertz-quartz**

---

## What This Is

Home Assistant custom integration for controlling Evertz EQX / EQT video
routers via a MAGNUM controller using the Quartz Remote Control Protocol
(Application Note 65).

---

## Integration File Map

```
custom_components/evertz_quartz/
  __init__.py          Entry setup, multi-router service, CSV name loading
  quartz_client.py     Asyncio TCP client for Quartz protocol
  config_flow.py       2-step setup UI — connect + CSV profile upload
  options_flow.py      Configure panel — resize, re-import CSV, debug settings
  select.py            Destination select entities + dual log level controls
  button.py            Resync buttons + Clear CSV Profile button
  diagnostics.py       HA diagnostics download
  csv_parser.py        MAGNUM profile_availability.csv parser
  helpers.py           effective() and router_display_name() shared helpers
  services.yaml        HA service schema for evertz_quartz.route
  const.py             All constants and defaults
  strings.json / translations/en.json
  brand/icon.png
```

---

## Critical Protocol Facts (MAGNUM-specific)

**Port:** 6666

**CR-only line endings:** Quartz terminates messages with `\r` (0x0D) only.
Use `readuntil(b'\r')` not `readline()`. This was the root cause of .UV
updates never being received when we used readline().

**Order numbers, not Port Numbers:** MAGNUM communicates entirely in the
`Order` column from profile_availability.csv — NOT the `Port Number`
(Quartz crosspoint address). MAGNUM handles Port Number translation
internally. Port numbers are stored for diagnostics only.

```
.UV1,360   = destination Order=1 routed to source Order=360
.SVV001,360 = route destination Order=1 to source Order=360
```

**No mnemonic responses:** MAGNUM ignores `.RT`/`.RD` name queries.
Names come from CSV only.

**No `.QL` command:** Use `.I{level}{dest}` to interrogate routes.
Response: `.A{level}{dest},{src}`. MAGNUM may also ignore `.I`.

**`.UV` updates work:** MAGNUM sends unsolicited route updates on any
take made from the MAGNUM UI or other controllers.

**Optimistic routing:** `client.routes` updated immediately on `.SV` send.
MAGNUM may or may not echo `.UV` back after an HA-initiated take
(still under test — see TEST_PLAN.md).

**No keepalive needed:** MAGNUM holds TCP connections open. Invalid
commands cause `.E` responses or disconnection.

---

## CSV Profile Format

```
Device Short Name, Src or Dst, Port Number, Global Name, Hidden?, Order
VP, SRC, 1, 57CAM1, 0, 1
VP, DST, 323, QC4720, 0, 1
```

- **Order** = MAGNUM's sequential profile index — used in all protocol commands
- **Port Number** = Quartz crosspoint address — stored for diagnostics only
- 438 sources in the tested profile have Order ≠ Port (tieline/remote sources)
- Tested profile: 1164 sources, 1 destination (QC4720, Order=1, Port=323)

---

## Architecture

### Names
- CSV names stored in `entry.data` when uploaded (`csv_loaded=True`)
- Loaded from `entry.data` on every HA startup when `csv_loaded=True`
- If no CSV: `.RT`/`.RD` queries sent on connect (non-MAGNUM routers only)
- Names keyed by **Order**, not Port Number

### Routing
- `.SV{level}{dest_order},{src_order}\r` sent to route
- Optimistic state updated immediately in `client.routes`
- `.UV{level}{dest_order},{src_order}` received for external changes
- `.I{level}{dest_order}` sent on connect to query current state

### CSV re-import always reloads
Any CSV import via the Configure panel triggers a full HA reload.
Source Order values may shift even if counts are unchanged (profile reordering).
Counts are written to `entry.data` before reload so they are available immediately.

### Hybrid per-router logging
Each QuartzClient has a named logger:
  `custom_components.evertz_quartz.quartz_client.{router_name}`
Every log message also carries a `[RouterName]` prefix for at-a-glance
identification in multi-router setups.

The Client Log Level entity sets level on both the router-specific logger
and the base `quartz_client` logger.

### Multi-router service
```yaml
service: evertz_quartz.route
data:
  device_id: "abc123"      # HA device registry ID
  # OR
  router_name: "CR47"      # matches CONF_NAME or IP (case-insensitive)
  destination: 1           # Order index
  source: 360              # Order index
  levels: "V"              # optional
```

### Port Maps
`source_port_map` and `destination_port_map` (`{order: quartz_port}`)
stored in `entry.data` for diagnostics reference only. Never used in
routing commands.

---

## Entities (per router)

| Entity | Type | Category |
|---|---|---|
| `select.{name}_{dest}` | Select | — |
| `select.{name}_log_level` | Select | Diagnostic |
| `select.{name}_client_log_level` | Select | Diagnostic |
| `button.{name}_resync_all` | Button | Diagnostic |
| `button.{name}_resync_routes` | Button | Diagnostic |
| `button.{name}_resync_names` | Button | Diagnostic |
| `button.{name}_clear_csv` | Button | Diagnostic |

---

## Options Flow (Configure panel)

Fields: Max Sources, Max Destinations, Levels, Reconnect Delay,
Connect Timeout, CSV Upload

- Any CSV upload → full reload
- Levels / reconnect / timeout changes → apply live without reload
- Max Sources or Destinations change without CSV → reload

---

## Protocol Trace & Diagnostics

`client.stats` includes:
- `sv_sent`, `interrogate_sent`, `interrogate_replied`, `route_updates_uv`
- `last_rx_time`, `last_uv_time`, `last_sv_time`
- `unhandled` — count of unrecognised messages
- `trace` — ring buffer of last 100 TX/RX lines with ms timestamps

Available in Settings → Devices → Evertz Quartz → Download Diagnostics.

---

## Known Limitations / Open Questions

- Does MAGNUM respond to `.I` interrogate on connect? (test 1.2 — unknown)
- Does MAGNUM echo `.UV` after an HA-initiated `.SV`? (test 3.3 — unknown)
- 1164 sources in a dropdown is functional but not ideal — use the Lovelace card
- HomeKit Bridge: exclude `evertz_quartz` domain to avoid IID collisions

---

## Test Plan

See `TEST_PLAN.md` and the interactive test runner artifact in Claude.ai.
The test runner calls the Claude API with full integration context and
returns PASS/FAIL verdicts with specific findings.

**Key tests to run first:**
1. Test 1.2 — does MAGNUM respond to `.I`?
2. Test 2.1 — do `.UV` updates arrive from external route changes?
3. Test 3.1 — does routing from HA actually switch the physical router?
4. Test 3.3 — does MAGNUM echo `.UV` after an HA-initiated take?

---

## Rules

- **Never put real IP addresses, source names, destination names, or
  entity IDs in test files, documentation examples, or Claude artifacts.**
  Use generic placeholders: `router.local`, `MY-ROUTER`, `DEST-A`,
  `SRC-001`, `select.myrouter_dest_a`.

- All routing uses Order numbers. Never use Port Numbers in commands.

- CSV import always reloads. Never apply CSV data live without reload.
