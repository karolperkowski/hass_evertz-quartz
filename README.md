# Evertz Quartz Router — Home Assistant Integration

[![HACS Custom Repository](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

A Home Assistant custom integration for controlling **Evertz EQX / EQT video routers** using the **Quartz Remote Control Protocol** over TCP.

## Features

- **One `select` entity per destination** — choose any source from a dropdown
- **Real-time push updates** — unsolicited `.UV` messages from the router update HA state instantly
- **Mnemonic names** — source and destination labels are pulled from the router on startup
- **`evertz_quartz.route` service** — call from automations or scripts with optional level override
- **Auto-reconnect** — transparently reconnects after a network interruption
- **HACS compatible**

---

## Supported Devices

| Device | Protocol | Notes |
|---|---|---|
| Evertz EQX series | Quartz over TCP | Direct connection |
| Evertz EQT series | Quartz over TCP/Serial | TCP recommended |
| Evertz MAGNUM controller | Quartz over TCP | Supports up to 26 levels |
| Evertz EMR series | Quartz over TCP | Ports 3737–3740 |

---

## Installation

### Via HACS (recommended)

1. In HACS, go to **Integrations → Custom repositories**
2. Add `https://github.com/karolperkowski/hass_evertz-quartz` as an **Integration**
3. Search for "Evertz Quartz" and install
4. Restart Home Assistant

### Manual

Copy the `custom_components/evertz_quartz/` folder into your HA `config/custom_components/` directory and restart.

---

## Configuration

Go to **Settings → Devices & Services → Add Integration** and search for **Evertz Quartz Router**.

| Field | Description | Default |
|---|---|---|
| **IP Address** | Router or MAGNUM controller IP | — |
| **TCP Port** | Quartz control port | `3737` |
| **Max Sources** | Number of sources configured in Quartz | `32` |
| **Max Destinations** | Number of destinations configured in Quartz | `32` |
| **Levels** | Routing level(s) to switch (e.g. `V`, `VA`, `VABC`) | `V` |

> **Note:** Source and destination numbers in Quartz may differ from the router's internal crosspoint numbers if the Quartz interface is configured with an offset.

---

## Common TCP Ports

| Router / Controller | Default Port |
|---|---|
| Evertz EMR series | 3737–3740 |
| Evertz EQX (Quartz interface) | 3737 |
| Quartz legacy / Telnet | 23 |
| Evertz MAGNUM | 3737 |

---

## Service: `evertz_quartz.route`

Route a source to a destination from an automation or script:

```yaml
service: evertz_quartz.route
data:
  destination: 3       # 1-based destination number
  source: 1            # 1-based source number
  levels: "V"          # optional — overrides the configured default
```

---

## Levels Reference

| Level string | Meaning |
|---|---|
| `V` | Video only |
| `VA` | Video + Audio A |
| `VABC` | Video + Audio A/B/C |
| `V,A,B,C,D,E,F,G` | All 8 levels |

---

## Protocol Notes

The **Quartz Remote Control Protocol** is an open ASCII-based protocol operating over TCP (or serial). Key messages:

| Message | Direction | Meaning |
|---|---|---|
| `.SV[lvl][dst],[src]\r` | → Router | Set route |
| `.UV[lvl][dst],[src]\r` | ← Router | Unsolicited route update |
| `.QL[lvl][dst]\r` | → Router | Query current route |
| `.RD[dst]\r` | → Router | Read destination mnemonic |
| `.RT[src]\r` | → Router | Read source mnemonic |
| `.A\r` | ← Router | Acknowledge |

---

## License

MIT


---

## Profile CSV Import

The integration uses MAGNUM's `profile_availability.csv` export to populate source and destination names and counts. Without a CSV the integration works but entities show generic names (`Source 1`, `Destination 1`).

### Exporting from MAGNUM

In your MAGNUM web interface, export the profile availability CSV for the profile you want to control. The file has this format:

```
Device Short Name,Src or Dst,Port Number,Global Name,Hidden?,Order
VP,SRC,1,57CAM1,0,1
VP,DST,323,QC4720,0,1
```

The integration uses the **Order** column for entity counts and routing — this is MAGNUM's sequential profile index. Port Number is stored for diagnostics only.

### Uploading during setup

In the **Router Profile** step of the setup wizard, use the **Profile CSV** file picker to upload your export. The integration saves immediately with the correct counts and names.

### Uploading after setup

Go to **Settings → Devices & Services → Evertz Quartz → Configure** and use the **Re-import Profile CSV** file picker. You will see a diff summary of what will change before it is applied.

### Clearing the CSV

Press the **Clear CSV Profile** button on the device card to revert to querying the router directly for names. On MAGNUM this will result in generic names since MAGNUM does not respond to Quartz mnemonic queries.

---

## Debug & Diagnostics

### Enabling debug logging

Add this to your `configuration.yaml` to turn on debug-level logs for the integration:

```yaml
logger:
  default: warning
  logs:
    custom_components.evertz_quartz: debug
    custom_components.evertz_quartz.quartz_client: debug
```

Restart Home Assistant, then check **Settings → System → Logs**.

### Verbose TCP logging (log every message)

For deeper protocol-level tracing — every raw Quartz TX/RX frame logged — enable **Verbose TCP logging** without restarting:

1. Go to **Settings → Devices & Services**
2. Click **Evertz Quartz Router → Configure**
3. Toggle **Verbose TCP logging** on and click Submit

You'll see entries like:
```
TX → .SVV003,001
RX ← .UVV003,001
RX ← .A
```

Turn it off the same way once you're done — it's chatty on busy routers.

### Download diagnostics

Home Assistant's built-in diagnostics dump captures a full JSON snapshot:
- Connection state (connected, reconnect count, timestamps)
- All current routes
- All source and destination names
- Message counters and the last 20 errors
- All active options

To download it: **Settings → Devices & Services → Evertz Quartz Router → ⋮ → Download diagnostics**

### Connection options (Configure panel)

| Option | Default | Description |
|---|---|---|
| **Verbose TCP logging** | Off | Log every raw TX/RX Quartz frame at DEBUG level |
| **Reconnect delay** | 5s | Wait time before reconnecting after a drop |
| **Connection timeout** | 10s | Max time to establish the initial TCP connection |

All options apply immediately — no Home Assistant restart required.

---

## Entities

### Routing (per destination)

| Entity | Type | Description |
|---|---|---|
| `select.destination_N` | Select | Routes a source to this destination. Options list uses mnemonic names when available. |

### Diagnostic entities

These appear under the device card in Settings → Devices & Services and are also available in automations and dashboards.

| Entity | Type | Description |
|---|---|---|
| `select.log_level` | Select | Set the integration log level: **Debug / Info / Warning / Error**. Takes effect instantly — no restart or `configuration.yaml` edit needed. |
| `button.resync_all` | Button | Re-polls both mnemonic names and current routes from the router. |
| `button.resync_routes` | Button | Re-polls current crosspoint state only. |
| `button.resync_names` | Button | Re-polls source and destination names only. |

### When to use each resync button

- **Resync All** — after a router config change (new sources/destinations, renamed ports)
- **Resync Routes** — if HA state gets out of sync (e.g. after a reconnect)
- **Resync Names** — if mnemonic labels changed on the router but routes are correct

### Log Level

The **Log Level** select entity lets you change the verbosity of the integration without editing `configuration.yaml`:

| Level | Use |
|---|---|
| **Warning** (default) | Normal operation — only problems logged |
| **Info** | Connection events, resync actions, option changes |
| **Debug** | Mnemonic parsing, route change detail, keepalives |
| **Debug** + Verbose TCP | Every raw `.SV` / `.UV` / `.A` frame logged (set Verbose TCP in Configure) |

> The level resets to `Warning` on HA restart. For a permanent setting, add the `logger` block to `configuration.yaml` as described in the Debug section above.
