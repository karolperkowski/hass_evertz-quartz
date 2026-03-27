# Evertz Quartz Router — Integration Test Plan

**Version:** 1.8.1  
**Target system:** MAGNUM controller at 10.150.47.11:6666  
**Profile:** 1164 sources, 1 destination (QC4720, Order=1)  
**HA minimum:** 2024.6.0

---

## How to run these tests

1. Set **Client Log Level** to `DEBUG` on the router device card
2. Set **Log Level** to `DEBUG`
3. Open **Settings → System → Logs** in a separate browser tab — filter by `evertz_quartz`
4. Work through each section in order — earlier tests are prerequisites for later ones

Pass/fail criteria are listed under each test. Record any unexpected log output.

---

## 1. Connection

### 1.1 Initial connect
**Steps:**
1. Reload the integration (Settings → Devices & Services → Evertz Quartz → three dots → Reload)
2. Watch logs

**Expected logs:**
```
DEBUG Connecting to 10.150.47.11:6666 (timeout 10s)
INFO  Connected to Evertz Quartz router at 10.150.47.11:6666 (connection #1)
INFO  Evertz Quartz [CR47] connected
DEBUG Interrogating route state for destination order(s): [1]
DEBUG TX → .IVV001   (or .IV1)
DEBUG Listening for messages from 10.150.47.11:6666
```

**Pass:** Connected log appears, no ERROR lines  
**Fail:** `ConfigEntryNotReady`, timeout, or repeated reconnect attempts

---

### 1.2 Route state on connect
**Steps:**
1. After 1.1, check the `select.cr47_qc4720` entity state in Developer Tools → States

**Expected:**
- If MAGNUM responds to `.I`: entity shows the current active source name
- If MAGNUM ignores `.I`: entity state is `unknown` or blank

**Pass:** Either outcome is acceptable — record which one occurs  
**Note:** This determines whether we need a different startup query strategy

---

### 1.3 Reconnect after network interruption
**Steps:**
1. Confirm integration is connected (green badge on device card)
2. Block network to MAGNUM for 30 seconds (or restart MAGNUM if accessible)
3. Restore network
4. Watch logs

**Expected logs:**
```
INFO  Disconnected from 10.150.47.11:6666
INFO  Evertz Quartz [CR47] disconnected
DEBUG Connecting to 10.150.47.11:6666 ...
INFO  Connected to Evertz Quartz router at 10.150.47.11:6666 (connection #2)
```

**Pass:** Reconnects within `reconnect_delay` seconds (default 5s) + connect timeout (10s)  
**Fail:** Does not reconnect, or HA needs a manual reload

---

## 2. Receiving route updates from MAGNUM

### 2.1 External route change visible in HA
**Steps:**
1. Make a route change in the MAGNUM UI (route any source to QC4720)
2. Watch DEBUG logs
3. Check `select.cr47_qc4720` entity state

**Expected logs:**
```
DEBUG RX ← '.UV1,360'
DEBUG Route update: QC4720 (Order 1) → BARS (Order 360)
```

**Expected entity state:** Updates to the source name (e.g. `BARS`) within 1–2 seconds  
**Pass:** Entity `current_option` matches what MAGNUM shows  
**Fail:** No `RX ←` log line, or entity does not update

---

### 2.2 Multiple rapid route changes
**Steps:**
1. Make 5 rapid route changes in MAGNUM (different sources each time)
2. Check final entity state matches final MAGNUM state

**Pass:** Final state matches, no missed updates, no disconnect  
**Fail:** Entity shows stale route, or connection drops under rapid updates

---

## 3. Routing from HA

### 3.1 Route via select entity
**Steps:**
1. Go to Developer Tools → States
2. Find `select.cr47_qc4720`
3. Change the state to a known source (e.g. `57CAM1`)
4. Watch DEBUG logs
5. Verify on the physical router/MAGNUM that the route changed

**Expected logs:**
```
DEBUG TX → .SVV001,001
DEBUG Optimistic route: dest Order=1 → src Order=1 (was X)
```

**Pass:** Physical router switches to 57CAM1  
**Fail:** No `TX →` log, or router does not switch

---

### 3.2 Optimistic state update
**Steps:**
1. Immediately after 3.1, check `select.cr47_qc4720` state in HA
2. Do NOT wait for a `.UV` echo

**Expected:** Entity already shows `57CAM1` (optimistic update applied)  
**Pass:** State updated immediately on send, before any `.UV` arrives  
**Fail:** State stays at previous source until `.UV` arrives (or never)

---

### 3.3 `.UV` echo after HA-initiated take
**Steps:**
1. Make a route from HA (as in 3.1)
2. Watch logs for a subsequent `.UV` message

**Expected (either outcome acceptable — record which):**
- MAGNUM echoes `.UV1,001` confirming the take → state confirmed
- MAGNUM is silent → optimistic state is the only update

**Note:** This determines whether we need to rely on optimistic routing permanently

---

### 3.4 Route via service call
**Steps:**
1. Go to Developer Tools → Services
2. Call `evertz_quartz.route` with:
   ```yaml
   destination: 1
   source: 360
   ```
3. Verify physical router switches to BARS (Order=360, Port=403)

**Pass:** Router switches, entity updates to `BARS`  
**Fail:** Service call errors, or wrong source routed

---

### 3.5 Route an Order>199 source (non-contiguous port)
**Steps:**
1. Route to `49CAM1` (Order=200, Port=201) via the select entity or service
2. Watch the `.SV` command in DEBUG logs

**Expected log:**
```
DEBUG TX → .SVV001,200
```
(Order 200, not Port 201)

**Pass:** Log shows Order=200, physical router switches to 49CAM1  
**Fail:** Log shows Port=201, or router routes to wrong source

---

## 4. CSV Profile

### 4.1 Names loaded on startup
**Steps:**
1. Reload the integration
2. Immediately check `select.cr47_qc4720` entity attributes in Developer Tools

**Expected:** `options` attribute lists `57CAM1`, `57CAM2` etc. (not `Source 1`, `Source 2`)  
**Pass:** Named options visible immediately on startup without querying router  
**Fail:** Options show generic names, or names appear only after a delay

---

### 4.2 Re-import CSV via Configure
**Steps:**
1. Settings → Devices & Services → Evertz Quartz → Configure
2. Upload `profile_availability.csv`
3. Review the diff summary shown
4. Confirm

**Expected:** Diff shows `1164 source names` and `1 destination name`  
**Pass:** Names still correct after reload, `csv_loaded: true` visible in diagnostics  
**Fail:** Diff shows no changes, or names revert to generic after reload

---

### 4.3 Clear CSV Profile button
**Steps:**
1. Press the `Clear CSV Profile` button on the device card
2. Check entity options

**Expected:** Options revert to `Source 1`, `Source 2` etc.  
**Pass:** Generic names shown, entity still routes correctly by Order index  
**Fail:** Options disappear entirely, or integration errors

---

## 5. Lovelace Card

### 5.1 Card loads
**Steps:**
1. Add resource `/hacsfiles/hass_evertz-quartz/evertz-quartz-card.js`
2. Add card to dashboard:
   ```yaml
   type: custom:evertz-quartz-card
   title: CR47
   destinations:
     - entity: select.cr47_qc4720
       name: QC4720
   ```
3. Open dashboard

**Expected:** Card renders with header, favourites view, source list  
**Pass:** No "Custom element doesn't exist" error in browser console  
**Fail:** Card shows error or blank

---

### 5.2 Source list populated
**Steps:**
1. Open the card
2. Scroll through All Sources list

**Expected:** 1164 sources listed, named correctly (57CAM1, not Source 1)  
**Pass:** Names match CSV  
**Fail:** Generic names, or wrong count

---

### 5.3 Route via card
**Steps:**
1. Click any source in the Favourites or All Sources list
2. Confirm in the dialog
3. Verify physical router switches

**Pass:** Router switches, card's active source banner updates  
**Fail:** Dialog appears but take does not execute, or no HA service call

---

### 5.4 Favourites persist
**Steps:**
1. Click ★ on 3 sources
2. Refresh the browser
3. Reopen the card

**Pass:** Starred sources still appear in Favourites section  
**Fail:** Favourites lost on page refresh

---

### 5.5 Matrix view
**Steps:**
1. Click the Matrix button in the card header
2. Verify sources appear as rows with QC4720 as a column
3. Click a cell to route

**Pass:** Matrix renders, active cell highlighted, route fires on click  
**Fail:** Matrix blank, or routing does not work from matrix view

---

### 5.6 Search
**Steps:**
1. Type `CAM` in the search box
2. Verify only camera sources appear

**Pass:** Results filter immediately, no lag  
**Fail:** All sources still shown, or browser freezes on keypress

---

## 6. Diagnostics

### 6.1 Download diagnostics
**Steps:**
1. Settings → Devices & Services → Evertz Quartz → device → Download Diagnostics

**Expected JSON includes:**
```json
{
  "connection": { "connected": true, ... },
  "routes": { "1": <src_order> },
  "source_names": { "1": "57CAM1", ... },
  "destination_names": { "1": "QC4720" }
}
```

**Pass:** JSON downloads, contains correct data  
**Fail:** Error downloading, or names missing

---

### 6.2 Log level persistence
**Steps:**
1. Set Client Log Level to `DEBUG`
2. Restart Home Assistant
3. Check that DEBUG logs still appear without re-setting the level

**Pass:** DEBUG level active after restart  
**Fail:** Reverts to WARNING after restart

---

## 7. Edge cases

### 7.1 Unknown source routed externally
**Steps:**
1. From MAGNUM, route a source that does NOT exist in the CSV profile
2. Check HA entity state

**Expected:** Entity shows `Source <N>` (fallback label using Order number)  
**Pass:** Entity does not error, shows fallback name  
**Fail:** Entity shows blank, unavailable, or errors in log

---

### 7.2 HA restart with active connection
**Steps:**
1. With MAGNUM connected, restart HA
2. After restart, check connection status and entity state

**Pass:** Reconnects automatically, entity options populated from CSV  
**Fail:** Stuck in connecting, or entities unavailable

---

### 7.3 Multiple rapid HA restarts
**Steps:**
1. Restart HA 3 times in quick succession
2. Check for orphaned TCP connections or errors

**Pass:** Clean connect each time, no `connection #N` growing unexpectedly  
**Fail:** MAGNUM rejects connection (too many open sockets)

---

## Test Results Template

```
Date:
HA Version:
Integration Version: 1.8.1
Tester:

1.1 Initial connect:          PASS / FAIL — notes:
1.2 Route state on connect:   PASS / FAIL — MAGNUM responds to .I: YES / NO
1.3 Reconnect:                PASS / FAIL — notes:

2.1 External route change:    PASS / FAIL — notes:
2.2 Rapid changes:            PASS / FAIL — notes:

3.1 Route via select:         PASS / FAIL — notes:
3.2 Optimistic update:        PASS / FAIL — notes:
3.3 .UV echo after HA take:   PASS / FAIL — MAGNUM echoes .UV: YES / NO
3.4 Route via service:        PASS / FAIL — notes:
3.5 Non-contiguous source:    PASS / FAIL — notes:

4.1 Names on startup:         PASS / FAIL — notes:
4.2 Re-import CSV:            PASS / FAIL — notes:
4.3 Clear CSV:                PASS / FAIL — notes:

5.1 Card loads:               PASS / FAIL — notes:
5.2 Source list:              PASS / FAIL — notes:
5.3 Route via card:           PASS / FAIL — notes:
5.4 Favourites persist:       PASS / FAIL — notes:
5.5 Matrix view:              PASS / FAIL — notes:
5.6 Search:                   PASS / FAIL — notes:

6.1 Diagnostics download:     PASS / FAIL — notes:
6.2 Log level persistence:    PASS / FAIL — notes:

7.1 Unknown source:           PASS / FAIL — notes:
7.2 Restart with connection:  PASS / FAIL — notes:
7.3 Multiple rapid restarts:  PASS / FAIL — notes:
```
