# Evertz Quartz Router — Home Assistant Integration

[![HACS Custom Repository](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

Control **Evertz EQX / EQT video routers** from Home Assistant using the Quartz Remote Control Protocol over TCP.

> **New to this?** This integration talks to your router (or MAGNUM controller) over the network. Once installed, you get a dropdown entity per destination that lets you switch sources — and changes made on the router reflect back in HA automatically.

---

## Related Repository

| Repo | Purpose |
|---|---|
| **hass_evertz-quartz** (this repo) | HA integration — entities, service, config |
| **[lovelace-evertz-quartz](https://github.com/karolperkowski/lovelace-evertz-quartz)** | Optional Lovelace card — better UI for large routers |

---

## Supported Devices

| Device | Protocol | Notes |
|---|---|---|
| Evertz EQX series | Quartz over TCP | Direct connection |
| Evertz EQT series | Quartz over TCP | TCP recommended |
| Evertz MAGNUM controller | Quartz over TCP | Port 6666, uses profile Order numbers |
| Evertz EMR series | Quartz over TCP | Ports 3737–3740 |

---

## Prerequisites

- Home Assistant 2024.6.0 or newer
- HACS installed ([hacs.xyz](https://hacs.xyz))
- Network access from HA to the router or MAGNUM controller
- TCP port open on the router (see [Common TCP Ports](#common-tcp-ports))

---

## Installation

### Step 1 — Add the integration via HACS

1. Open HACS → **Integrations**
2. Click ⋮ → **Custom repositories**
3. Add `https://github.com/karolperkowski/hass_evertz-quartz` as category **Integration**
4. Search for **Evertz Quartz Router** and click **Download**
5. Restart Home Assistant

### Step 2 — Add your router

1. Go to **Settings → Devices & Services → Add Integration**
2. Search for **Evertz Quartz Router**
3. Fill in the connection details (see [Configuration](#configuration))

> **MAGNUM users:** Use port `6666` and upload your `profile_availability.csv` in the second step. This gives your sources and destinations their real names instead of generic labels.

---

## Configuration

### Step 1 of 2 — Connection

| Field | Description | Example |
|---|---|---|
| **IP Address** | Router or MAGNUM controller IP | `192.168.1.100` |
| **TCP Port** | Quartz control port | `6666` (MAGNUM) / `3737` (direct) |
| **Router Name** | Friendly name shown in HA | `Studio Router` |

### Step 2 of 2 — Profile

| Field | Description | Default |
|---|---|---|
| **Max Sources** | How many sources the router has | `32` |
| **Max Destinations** | How many destinations the router has | `32` |
| **Levels** | Signal level(s) to switch | `V` |
| **Profile CSV** | Optional — upload MAGNUM's `profile_availability.csv` | — |

> **Tip:** If you upload a CSV, Max Sources and Max Destinations are set automatically. You do not need to fill them in manually.

---

## Common TCP Ports

| Router / Controller | Default Port |
|---|---|
| Evertz MAGNUM | `6666` |
| Evertz EQX (direct) | `3737` |
| Evertz EMR series | `3737` – `3740` |
| Legacy / Telnet | `23` |

---

## Profile CSV (MAGNUM)

MAGNUM exports a `profile_availability.csv` file that contains all your source and destination names. Without it, entities show generic labels like `Source 1`, `Source 2`.

### How to export from MAGNUM

In the MAGNUM web interface, export the profile availability CSV for the profile you want to control. It looks like this:

```
Device Short Name,Src or Dst,Port Number,Global Name,Hidden?,Order
VP,SRC,1,CAMERA-1,0,1
VP,DST,323,MON-A,0,1
```

The integration uses the **Order** column — MAGNUM's sequential profile index — for all routing commands. The Port Number column is stored for diagnostics only.

### Upload during setup

Upload the file in Step 2 of the setup wizard. The integration reads it immediately and saves the correct counts and names.

### Upload after setup (profile changed)

If your MAGNUM profile changes — sources added, removed, or reordered:

1. **Settings → Devices & Services → Evertz Quartz → Configure**
2. Upload the updated CSV
3. Review the diff summary (shows what changed)
4. Confirm — the integration reloads automatically

> **Why does it reload?** Source Order values can shift even if the total count stays the same. A full reload ensures all entities are consistent with the new profile.

### Clear the CSV

Press the **Clear CSV Profile** button on the device card to remove the loaded profile. Entities revert to generic names. Routing still works — it always uses Order numbers regardless of names.

---

## Service: `evertz_quartz.route`

Route a source to a destination from an automation or script.

```yaml
service: evertz_quartz.route
data:
  destination: 1       # destination Order number (from CSV)
  source: 5            # source Order number (from CSV)
  levels: "V"          # optional — overrides the configured default
```

### Multiple routers

If you have more than one router configured, you must specify which one:

```yaml
service: evertz_quartz.route
data:
  router_name: "Studio Router"   # name you gave at setup
  destination: 1
  source: 5
```

Or use the HA device ID (found at Settings → Devices → your router → device info):

```yaml
service: evertz_quartz.route
data:
  device_id: "abc123def456"
  destination: 1
  source: 5
```

---

## Levels Reference

| Level string | Meaning |
|---|---|
| `V` | Video only |
| `VA` | Video + Audio A |
| `VABC` | Video + Audio A, B, and C |

---

## Entities

### Per destination

| Entity | Type | Description |
|---|---|---|
| `select.{name}_{destination}` | Select | Route any source to this destination. Options show CSV names (or generic names without a CSV). Hidden profile rows are excluded; duplicate names get an ` (Order N)` suffix so every option is unambiguous. |
| `lock.{name}_{destination}_lock` | Lock | Lock/unlock the destination (`.BL` / `.BU`). Locked destinations reject takes from HA. Attributes show the lock type — `software` (clearable from HA) or `panel:N` (hardware Q-link panel lock, must be released at the panel). |
| `sensor.{name}_{destination}_source` | Sensor | Read-only view of the destination's current source. Created only for destinations marked **read-only** in the Configure panel. |

### Status (per router)

| Entity | Type | Description |
|---|---|---|
| `binary_sensor.{name}_connected` | Binary sensor | Live TCP connection status. |
| `binary_sensor.{name}_profile_mismatch` | Binary sensor | ON when the router uses Order numbers beyond the configured size, or when fewer destinations answered interrogation than configured (over-provisioned profile). Attributes include detected/suggested counts and a resolution hint. |
| `sensor.{name}_last_connected` | Sensor | Timestamp of the last successful connection (diagnostic). |
| `sensor.{name}_profile` | Sensor | Profile summary — configured size, CSV status, name counts (diagnostic). |

### Diagnostic controls (per router)

These appear under the device in **Settings → Devices & Services** and are also available in automations and dashboards.

| Entity | Type | Description |
|---|---|---|
| `select.{name}_log_level` | Select | Integration log level — Debug / Info / Warning / Error. Changes take effect immediately, no restart needed. |
| `select.{name}_client_log_level` | Select | TCP protocol log level — controls how much raw routing traffic is logged. |
| `button.{name}_resync_all` | Button | Re-polls both names and routes from the router. |
| `button.{name}_resync_routes` | Button | Re-polls current route state only. |
| `button.{name}_resync_names` | Button | Re-polls source and destination names (no effect on MAGNUM — use CSV instead). |
| `button.{name}_detect_destinations` | Button | Re-interrogates the controller to count real destinations, then reports the result in a notification. Useful when the profile is over-provisioned. |
| `button.{name}_clean_up_stale_entities` | Button | Removes orphaned destination entities left in the registry after the matrix was shrunk. |
| `button.{name}_clear_csv` | Button | Removes the loaded CSV profile and reverts to generic names. |

### When to use each resync button

- **Resync All** — after a router config change (new sources, destinations, renamed ports)
- **Resync Routes** — if HA state looks out of sync after a reconnect
- **Resync Names** — if labels changed on a non-MAGNUM router

---

## Read-Only Destinations & Permissions

Destinations selected under **Configure → Read-only destinations** get a
read-only source sensor, and **takes to them are blocked** — in the select
entity and the `evertz_quartz.route` service — unless the calling HA user is
in the **allowed users** list. The same rule applies to locking/unlocking the
destination. Calls with no user context (automations, scripts) are always
blocked on read-only destinations.

The select entity stays visible to everyone and exposes `read_only` and
`readonly_allowed_users` attributes so the Lovelace card can render it as
display-only for non-allowed users; enforcement always happens server-side.

> The read-only list is keyed by Order — re-check it after a CSV re-import if
> the profile order changed.

---

## Events

### `evertz_quartz_route_blocked`

Fired on the HA event bus whenever an operation is blocked, from every
enforcement path. A persistent notification is raised at the same time.

| Field | Values / meaning |
|---|---|
| `router` | Router display name |
| `entry_id` | Config entry ID |
| `reason` | `read_only` \| `locked` \| `cross_namespace` |
| `origin` | `select` (dropdown), `service` (`evertz_quartz.route`), `lock` (lock entity) |
| `action` | `route` \| `lock` \| `unlock` |
| `destination` / `destination_name` | Destination Order + name |
| `source` / `source_name` | Source Order + name (null for lock/unlock) |
| `user_id` | HA user that attempted the action (null for automations/scripts) |

Example — push a notification when someone hits a blocked destination:

```yaml
automation:
  - alias: "Notify on blocked route"
    trigger:
      - platform: event
        event_type: evertz_quartz_route_blocked
    action:
      - service: notify.mobile_app_my_phone
        data:
          title: "Route blocked on {{ trigger.event.data.router }}"
          message: >
            {{ trigger.event.data.action }} to
            {{ trigger.event.data.destination_name }} blocked
            ({{ trigger.event.data.reason }})
```

---

## Connection Behaviour

- **Startup sync notification** — on the first connect after a restart or
  reload, a persistent notification explains that routes/locks/names are still
  synchronizing (with a time estimate) and clears itself when the sync
  finishes. Destination entities may show *Unknown* until then.
- **Batched sync sweeps** — route, lock, and name interrogations are sent in
  batches, so even large matrices sync in seconds.
- **Reconnect backoff** — after repeated connection failures the reconnect
  delay doubles (starting from the configured value, capped at 120 s) and
  resets on success.
- **Rejected takes roll back** — if the router answers a take with `.E`, the
  optimistic dropdown state snaps back to the previous source.
- **Reconfigure** — change the router's IP/port/name via
  **Settings → Devices & Services → ⋮ → Reconfigure** without losing the
  profile, CSV names, or options.

---

## Lovelace Card

For routers with many sources, the default select entity dropdown can be unwieldy. The companion Lovelace card provides a much better interface:

- Favourites grid for quick access to common sources
- Full searchable source list with category filters
- Matrix view — all destinations as columns, all sources as rows
- Confirm-before-take dialog to prevent accidental routes

**Install separately:** [lovelace-evertz-quartz](https://github.com/karolperkowski/lovelace-evertz-quartz)

See that repo for full installation instructions. The card requires this integration to be installed and working first.

---

## Debug & Diagnostics

### Changing the log level

The easiest way is via the **Log Level** and **Client Log Level** select entities on the device card. No `configuration.yaml` edit or restart needed. Levels persist across HA restarts.

| Level | What you see |
|---|---|
| **Warning** (default) | Only problems — connection errors, routing failures |
| **Info** | Connection events, resyncs, config changes |
| **Debug** | Every TX / RX message, route updates, interrogate replies |

### Downloading diagnostics

**Settings → Devices & Services → Evertz Quartz → ⋮ → Download diagnostics**

The JSON file includes:

- Connection state (connected/disconnected, reconnect count, timestamps)
- All current routes and lock states (destination → source Order numbers)
- All loaded source and destination names
- Message counters: `.SV` sent, `.UV` received, `.I` interrogate
  sent/replied/rejected, mnemonic queries rejected
- Protocol trace — last 100 TX/RX lines with millisecond timestamps
- Last 20 errors

The router's IP address is redacted automatically, so the file is safe to
attach to a GitHub issue. Paste the `stats` and `protocol_trace` sections when
reporting a problem.

### Configure panel

**Settings → Devices & Services → Evertz Quartz → Configure**

| Option | Default | Description |
|---|---|---|
| **Levels** | `V` | Routing levels |
| **Reconnect delay** | 5s | Wait time before reconnecting after a drop (backoff floor) |
| **Connection timeout** | 10s | Max time to establish the TCP connection |
| **Read-only destinations** | — | Destinations that get a read-only sensor and blocked takes |
| **Allowed users** | — | HA users still permitted to control read-only destinations |
| **Max Sources** | 32 | Number of sources (set automatically from CSV) |
| **Max Destinations** | 32 | Number of destinations (set automatically from CSV) |
| **Profile CSV** | — | Re-import an updated MAGNUM profile |

---

## Protocol Notes

The **Quartz Remote Control Protocol** (Evertz Application Note 65) is an open ASCII protocol over TCP. Messages end with `\r` (carriage return only — not `\r\n`).

| Message | Direction | Meaning |
|---|---|---|
| `.SV[lvl][dst],[src]\r` | → Router | Set crosspoint |
| `.UV[lvl][dst],[src]\r` | ← Router | Unsolicited route update |
| `.I[lvl][dst]\r` | → Router | Interrogate current route |
| `.A[lvl][dst],[src]\r` | ← Router | Route interrogate reply |
| `.RD[dst]\r` | → Router | Read destination name |
| `.RT[src]\r` | → Router | Read source name |
| `.BL[dst]\r` / `.BU[dst]\r` | → Router | Lock / unlock destination |
| `.BI[dst]\r` | → Router | Interrogate lock state |
| `.BA[dst],[value]\r` | ← Router | Lock state (0 = unlocked, 255 = software lock, 1–254 = panel lock) |
| `.A\r` | ← Router | Generic acknowledge |
| `.E\r` | ← Router | Error |

**MAGNUM-specific behaviour:**

- Routes by `Order` number (sequential profile index), not Quartz Port Number
- Does not respond to `.RT` / `.RD` name queries — use the CSV instead
  (the integration aborts name sweeps automatically after a burst of `.E`)
- Sends `.UV` for all routes made from MAGNUM or other controllers
- Holds TCP connections open; after 60 s of silence the integration sends a
  `.I` keepalive probe to detect dead connections

---

## Test Plan

See [TEST_PLAN.md](TEST_PLAN.md) for the full test suite.

An interactive test runner is also available as a Claude.ai artifact — it calls the Claude API and gives PASS/FAIL verdicts automatically based on your log output.

---

## License

MIT
