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
  __init__.py          Entry setup, multi-router service, CSV name loading,
                       startup-sync notification, over-provision detection
  quartz_client.py     Asyncio TCP client for Quartz protocol
  config_flow.py       2-step setup UI + reconfigure step (host/port/name)
  options_flow.py      Configure panel — resize, re-import CSV, read-only lists
  select.py            Destination select entities + dual log level controls
  button.py            Resync / Detect Destinations / Clean Up / Clear CSV buttons
  lock.py              Destination lock entities (.BL/.BU/.BI/.BA)
  binary_sensor.py     Connected + Profile Mismatch sensors
  sensor.py            Last Connected, Profile summary, read-only source sensors
  diagnostics.py       HA diagnostics download (host redacted)
  csv_parser.py        MAGNUM profile_availability.csv parser
  helpers.py           effective(), router_display_name(), user_can_route(),
                       notify_blocked_route(), detection_status(),
                       subscribe_listener(), device_info()
  services.yaml        HA service schema for evertz_quartz.route
  const.py             All constants and defaults
  strings.json / translations/en.json   (hand-synced twins — CI asserts equality)
  brand/icon.png

tests/                 pytest suite (pytest-homeassistant-custom-component)
.github/workflows/     ci.yml (ruff+mypy+pytest), validate.yml (HACS+hassfest),
                       version-bump.yml, release.yml
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

**Keepalive probe:** MAGNUM holds TCP connections open, but after 60 s of
RX silence the client sends a `.I{level}1` probe to detect dead
connections. Invalid commands cause `.E` responses or disconnection.

---

## CSV Profile Format

```
Device Short Name, Src or Dst, Port Number, Global Name, Hidden?, Order
VP, SRC, 1, 57CAM1, 0, 1
VP, DST, 323, QC4720, 0, 1
```

- **Order** = MAGNUM's sequential profile index — used in all protocol commands
- **Port Number** = Quartz crosspoint address — stored for diagnostics only
- **Hidden? = 1** rows are kept in the name/port maps (MAGNUM still uses their
  Orders in `.UV`/`.SV`) but excluded from the source dropdown options; their
  Orders are persisted in `entry.data` (`hidden_source_orders` /
  `hidden_destination_orders`) and mirrored to `client.hidden_sources` /
  `client.hidden_destinations`
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
- Optimistic state updated immediately in `client.routes`; an `.E` within 5 s
  of the take rolls it back to the previous source (a `.UV` echo disarms the
  rollback)
- `.UV{level}{dest_order},{src_order}` received for external changes
- `.I{level}{dest_order}` sent on connect to query current state

### Connect-time sync & .E attribution
- The reader task runs concurrently with the connect-time sweeps; sweeps are
  batched (`SWEEP_BATCH=8` commands per drain, 50 ms between batches)
- `.E` replies are attributed best-effort: pending take (rollback) →
  outstanding `.I` (`interrogate_rejected` — expected on over-provisioned
  profiles) → outstanding mnemonic query (`mnemonic_rejected`) → real error
  (`recent_errors`)
- Mnemonic sweeps (`.RD`/`.RT`) abort after 5 consecutive `.E` — MAGNUM
  rejects them all, and the guard prevents a 1000+-message error storm
- Over-provision detection runs after the sync sweep completes (driven by
  `sync_callback`), not on a wall-clock timer
- Reconnect delay backs off exponentially (configured value → ×2 per failed
  cycle → 120 s cap), reset on success

### Select options
- Per-entity cached options list + `label → Order` reverse map, invalidated
  on mnemonic/name updates; duplicate Global Names get an ` (Order N)` suffix
  so labels resolve unambiguously; hidden sources are filtered out

### Startup sync notification
On the first connect after startup/reload, a persistent notification tells the
user routes/locks/names are still synchronizing and entities may show Unknown
(estimate from `client.estimated_sync_seconds()`, derived from sweep pacing).
The client fires `sync_callback` when the connect-time sweep has been sent;
the notification is dismissed ~2 s later (reply grace), on disconnect, and on
unload. Reconnects during the same session do not re-announce.

### CSV re-import always reloads
Any CSV import via the Configure panel triggers a full HA reload.
Source Order values may shift even if counts are unchanged (profile reordering).
Counts are written to `entry.data` before reload so they are available immediately.

### One entry per router
Config entries carry a `{host}:{port}` unique_id — adding the same endpoint
twice aborts with `already_configured`. Host/port/name can be changed later
via the **Reconfigure** menu item (profile data, CSV names, and options are
preserved; the entry reloads).

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
| `lock.{name}_{dest}_lock` | Lock | — |
| `sensor.{name}_{dest}_source` | Sensor | — (read-only destinations only) |
| `binary_sensor.{name}_connected` | Binary sensor | — |
| `binary_sensor.{name}_profile_mismatch` | Binary sensor | Diagnostic |
| `sensor.{name}_last_connected` | Sensor | Diagnostic |
| `sensor.{name}_profile` | Sensor | Diagnostic |
| `select.{name}_log_level` | Select | Diagnostic |
| `select.{name}_client_log_level` | Select | Diagnostic |
| `button.{name}_resync_all` | Button | Diagnostic |
| `button.{name}_resync_routes` | Button | Diagnostic |
| `button.{name}_resync_names` | Button | Diagnostic |
| `button.{name}_detect_destinations` | Button | Diagnostic |
| `button.{name}_clean_up_stale_entities` | Button | Diagnostic |
| `button.{name}_clear_csv` | Button | Diagnostic |

### Read-only destinations
Destinations selected in the Configure panel (`readonly_destinations`
option, stored as Order strings) get a read-only sensor showing the
current source name. Takes to them are blocked — in the select entity
and the `evertz_quartz.route` service — unless the calling HA user is
in `readonly_allowed_users`. Calls with no user context (automations,
scripts) are always blocked on read-only destinations. The select
entity is still visible to all users; enforcement happens on the take.
The read-only list is keyed by Order, so re-check it after a CSV
re-import if the profile order changed.

The destination select entity exposes `read_only` and
`readonly_allowed_users` attributes so the Lovelace card can render
read-only destinations as display-only for the current frontend user
(`hass.user.id`). Enforcement always stays server-side.

### Blocked-route notifications
Every blocked operation — read-only, locked, or cross-namespace — goes through
`helpers.notify_blocked_route()`, which fires `evertz_quartz_route_blocked`
on the HA event bus and raises a persistent notification. Enforcement paths:
the destination select entity (`origin="select"`), the `evertz_quartz.route`
service (`origin="service"`), and the destination lock entity
(`origin="lock"`) — the latter two additionally raise `ServiceValidationError`
for the caller. Read-only enforcement covers **lock/unlock as well as takes**:
the same `user_can_route()` allowed-users list applies to the lock entity.
Event data: `router`, `entry_id`, `reason`
(`read_only`/`locked`/`cross_namespace`), `origin`, `action`
(`route`/`lock`/`unlock`), `destination`, `destination_name`, `source`,
`source_name`, `user_id`.

---

## Options Flow (Configure panel)

Fields: Levels, Reconnect Delay, Connect Timeout, Read-only Destinations,
Allowed Users, Max Sources, Max Destinations, CSV Upload

- Any CSV upload → full reload
- Levels / reconnect / timeout / allowed-users changes → apply live without reload
- Max Sources or Destinations change without CSV → reload
- Read-only destination list change → reload (sensors created/removed)

---

## Protocol Trace & Diagnostics

`client.stats` includes:
- `sv_sent`, `interrogate_sent`, `interrogate_replied`, `route_updates_uv`
- `interrogate_rejected`, `mnemonic_rejected` — expected `.E` replies,
  attributed instead of polluting `recent_errors`
- `last_rx_time`, `last_uv_time`, `last_sv_time`
- `unhandled` — count of unrecognised messages
- `trace` — ring buffer of last 100 TX/RX lines with ms timestamps

Available in Settings → Devices → Evertz Quartz → Download Diagnostics.
The router `host` is redacted in the download (safe for GitHub issues).

---

## Known Limitations / Open Questions

- Does MAGNUM respond to `.I` interrogate on connect? (test 1.2 — unknown)
- Does MAGNUM echo `.UV` after an HA-initiated `.SV`? (test 3.3 — unknown)
- 1164 sources in a dropdown is functional but not ideal — use the Lovelace card
- HomeKit Bridge: exclude `evertz_quartz` domain to avoid IID collisions

---

## Testing

**Automated:** `tests/` runs with pytest +
`pytest-homeassistant-custom-component` (`pip install -r requirements_test.txt`,
then `pytest`). CI (`.github/workflows/ci.yml`) runs ruff, mypy, pytest, and a
strings.json ↔ translations/en.json equality assert on every PR;
`validate.yml` runs HACS + hassfest validation. Keep tests passing and add
coverage with behavioral changes.

**On-router:** see `TEST_PLAN.md` and the interactive test runner artifact in
Claude.ai. The test runner calls the Claude API with full integration context
and returns PASS/FAIL verdicts with specific findings.

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

- **Bump the version on every commit.** Update `version` in
  `custom_components/evertz_quartz/manifest.json` in the same commit as any
  change — never leave it stale. Use semantic versioning: patch (`x.y.Z`) for
  fixes and docs, minor (`x.Y.0`) for new features/entities, major (`X.0.0`)
  for breaking changes. End the commit subject with the new version
  (e.g. `… v1.14.1`), matching existing history. HACS serves releases by
  version, so a stale version means users never receive the update. Enforced
  on PRs by `.github/workflows/version-bump.yml`; enable the local pre-commit
  check once with `git config core.hooksPath .githooks`. On merge to `main`,
  `.github/workflows/release.yml` auto-publishes a GitHub Release `v{version}`
  (the form HACS installs from) — no manual release step.
