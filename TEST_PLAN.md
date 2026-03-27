# Evertz Quartz Router — Test Plan

> **How to use this:** Work through each test in order. Earlier sections are prerequisites for later ones.
> Before starting, set both **Log Level** and **Client Log Level** to `DEBUG` on the router device card, then open **Settings → System → Logs** in a separate tab filtered by `evertz_quartz`.

**Integration:** [hass_evertz-quartz](https://github.com/karolperkowski/hass_evertz-quartz)
**Card:** [lovelace-evertz-quartz](https://github.com/karolperkowski/lovelace-evertz-quartz)
**HA minimum:** 2024.6.0

---

## 1. Connection

### 1.1 Initial connect

Reload the integration and confirm a clean TCP connection is established.

**Steps:**
1. Settings → Devices & Services → Evertz Quartz → ⋮ → Reload
2. Watch the logs

**Expected:**
```
DEBUG [ROUTER-NAME] Connecting to router.local:6666 (timeout 10s)
INFO  [ROUTER-NAME] Connected to Evertz Quartz router at router.local:6666 (connection #1)
INFO  Evertz Quartz [ROUTER-NAME] connected
DEBUG [ROUTER-NAME] TX → .IV1
DEBUG [ROUTER-NAME] Listening for messages from router.local:6666
```

✅ **Pass** — Connected log appears, no ERROR lines  
❌ **Fail** — `ConfigEntryNotReady`, timeout, or repeated reconnect attempts

---

### 1.2 Route state on connect

Does the controller respond to the `.I` interrogate sent on connect?

**Steps:**
1. Wait 5 seconds after test 1.1
2. Go to **Developer Tools → States** and check the destination select entity
3. Note the `state` value

**Expected:**
- If the controller responded: entity shows the current active source name
- If the controller ignored `.I`: entity state is `unknown` or blank

✅ **Pass (either outcome)** — Record which one. This determines whether we need a different startup strategy.

> **What to look for in logs:** `Route confirmed (.I reply)` or `Route sync (.I reply)` means the controller responded. If neither appears after the TX line, it was ignored.

---

### 1.3 Reconnect after interruption

The integration should automatically reconnect after a network drop.

**Steps:**
1. Confirm integration is connected (green status badge on device card)
2. Block network to the controller for 30 seconds
3. Restore network
4. Watch the logs

**Expected:**
```
INFO  [ROUTER-NAME] Disconnected from router.local:6666
INFO  Evertz Quartz [ROUTER-NAME] disconnected
INFO  [ROUTER-NAME] Connected to Evertz Quartz router at router.local:6666 (connection #2)
```

✅ **Pass** — Reconnects within ~15 seconds  
❌ **Fail** — Does not reconnect, or requires a manual reload

---

## 2. Receiving Updates

### 2.1 External route change visible in HA

When a route is changed on the controller, HA should update within 1–2 seconds.

**Steps:**
1. Ensure DEBUG logging is active
2. Make a route change on the controller — note which source you routed to
3. Watch the HA log for `RX ←` lines
4. Check the destination select entity state

**Expected:**
```
DEBUG [ROUTER-NAME] RX ← '.UV1,5'
DEBUG [ROUTER-NAME] Route update (.UV): DEST-A (Order 1) → SRC-005 (Order 5)
```

✅ **Pass** — Entity updates to match the controller within 1–2 seconds  
❌ **Fail** — No `RX ←` line appears, or entity does not update

---

### 2.2 Multiple rapid route changes

Make 5 rapid route changes on the controller and verify the final HA state matches.

**Steps:**
1. Make 5 quick route changes (different sources each time)
2. Wait 2 seconds after the last change
3. Check the entity state matches the final controller route

✅ **Pass** — All 5 updates received, final state matches  
❌ **Fail** — Missed updates, wrong final state, or connection dropped

---

## 3. Routing from HA

### 3.1 Route via select entity

Change a source from within HA and verify the physical router switches.

**Steps:**
1. Go to **Developer Tools → States**
2. Find the destination select entity
3. Set the state to any source name
4. Watch logs for a `TX →` line
5. Verify the physical router switches

**Expected:**
```
DEBUG [ROUTER-NAME] TX → .SVV001,001
DEBUG [ROUTER-NAME] Optimistic route: dest Order=1 → src Order=1 (was X)
```

✅ **Pass** — Physical router switches  
❌ **Fail** — No `TX →` line, or router does not switch

---

### 3.2 Optimistic state update

The entity should update immediately when `.SV` is sent — without waiting for confirmation from the controller.

**Steps:**
1. Immediately after 3.1, check the entity state in HA
2. Note whether it updated **before** any `.UV` line appears in the logs

✅ **Pass** — Entity shows new source at the same moment as the `TX →` log line  
❌ **Fail** — Entity waits for `.UV` before updating

---

### 3.3 `.UV` echo after HA take

Does the controller send a `.UV` confirmation after HA initiates a route? This is still an open question.

**Steps:**
1. Make a route from HA
2. Wait 3 seconds
3. Check logs for any `.UV` line appearing after the `.SV`

**Expected (record which one you see):**
- `RX ← '.UV...'` appears after the TX → optimistic routing is confirmed
- Nothing appears → optimistic routing is the only update (permanent behaviour)

> This result determines whether we need to change the routing architecture.

---

### 3.4 Route via service call

Test the `evertz_quartz.route` service with Order numbers directly.

**Steps:**
1. Go to **Developer Tools → Services**
2. Call `evertz_quartz.route` with `destination: 1` and `source: 5`
3. Verify the physical router routes to Order 5

**Expected:**
```
DEBUG [ROUTER-NAME] TX → .SVV001,005
```

✅ **Pass** — Router switches to the correct source  
❌ **Fail** — Service call errors, or wrong source routed

---

### 3.5 Non-contiguous source (Order ≠ Port)

For MAGNUM profiles where Order numbers do not match Port numbers, the `.SV` command must use the Order number — not the Port number.

**Steps:**
1. Find a source in your CSV where Order ≠ Port Number (common for tieline sources)
2. Route to it from the select entity or service
3. Check the TX log line shows the Order number, not the Port number

**Expected:**
```
DEBUG [ROUTER-NAME] TX → .SVV001,200    ← Order=200, not Port=201
```

✅ **Pass** — TX shows Order number, physical router routes to correct source  
❌ **Fail** — TX shows Port number — this is an Order/Port mapping bug

---

## 4. CSV Profile

### 4.1 Names loaded on startup

After a reload, entity options should immediately show CSV names.

**Steps:**
1. Reload the integration
2. Immediately go to **Developer Tools → States** and expand the destination entity attributes
3. Check the `options` list

✅ **Pass** — Options list shows real source names from the CSV immediately  
❌ **Fail** — Options show `Source 1`, `Source 2` etc. — check that `csv_loaded: true` appears in diagnostics

---

### 4.2 Re-import CSV via Configure

**Steps:**
1. Settings → Devices & Services → Evertz Quartz → Configure
2. Upload an updated `profile_availability.csv`
3. Review the diff summary
4. Confirm

✅ **Pass** — Diff shows correct counts and names, integration reloads, options correct after reload  
❌ **Fail** — Diff shows no changes, or names revert after reload

---

### 4.3 Clear CSV Profile button

**Steps:**
1. Press **Clear CSV Profile** on the device card
2. Check the destination entity options

✅ **Pass** — Options revert to `Source 1`, `Source 2` etc., routing still works  
❌ **Fail** — Options disappear entirely, or integration errors

---

## 5. Lovelace Card

> The card is in a separate repository: [lovelace-evertz-quartz](https://github.com/karolperkowski/lovelace-evertz-quartz)
> Install via HACS Dashboard category. Resource URL: `/hacsfiles/lovelace-evertz-quartz/evertz-quartz.js`

### 5.1 Card loads

**Steps:**
1. Add the JS resource and hard refresh (Ctrl+Shift+R)
2. Add the card to a dashboard:
   ```yaml
   type: custom:evertz-quartz-card
   title: My Router
   destinations:
     - entity: select.myrouter_dest_a
       name: DEST-A
   ```
3. Open DevTools console (F12)

✅ **Pass** — `EVERTZ-QUARTZ-CARD v1.0.0` appears in console, card renders  
❌ **Fail** — `Custom element doesn't exist: evertz-quartz-card` — resource not loaded

---

### 5.2 Source list populated

**Steps:**
1. Open the card
2. Check the All Sources list and footer count

✅ **Pass** — Footer shows correct source count, names match CSV  
❌ **Fail** — Wrong count or generic names (`Source N`)

---

### 5.3 Route via card

**Steps:**
1. Click any source button
2. Confirm in the dialog
3. Verify physical router switches

✅ **Pass** — Router switches, card banner updates to new source  
❌ **Fail** — Dialog appears but take does not execute

---

### 5.4 Favourites persist

**Steps:**
1. Click ★ on 3 sources
2. Hard refresh browser (Ctrl+Shift+R)
3. Reopen the card

✅ **Pass** — Same sources still in Favourites  
❌ **Fail** — Favourites lost after refresh

---

### 5.5 Matrix view

**Steps:**
1. Click **Matrix** in the card header
2. Verify destinations appear as column headers
3. Click a cell and confirm to route

✅ **Pass** — Matrix renders, active cell has green dot, routing works  
❌ **Fail** — Matrix is blank, or routing fails from matrix cells

---

### 5.6 Search

**Steps:**
1. Type a partial source name in the search box
2. Type something that matches nothing to test the empty state
3. Clear and verify all sources return

✅ **Pass** — Filtering is instant, empty state shown when nothing matches  
❌ **Fail** — Browser freezes, or filter does not work

---

## 6. Diagnostics

### 6.1 Download diagnostics

**Steps:**
1. Make at least one route change from HA
2. Settings → Devices → Evertz Quartz → ⋮ → Download Diagnostics
3. Open the JSON and check the `stats` and `routes` sections

**Expected JSON (excerpt):**
```json
{
  "stats": {
    "sv_sent": 1,
    "interrogate_sent": 1,
    "interrogate_replied": 0,
    "route_updates_uv": 0
  },
  "routes": { "1": 5 },
  "protocol_trace": ["11:30:00.123 TX .SVV001,005", "..."]
}
```

✅ **Pass** — JSON downloads with stats, routes, and protocol_trace  
❌ **Fail** — Error downloading, or sections missing

---

### 6.2 Log level persistence

**Steps:**
1. Set **Client Log Level** to `DEBUG`
2. Restart Home Assistant
3. Make a route change — check DEBUG logs appear without re-setting the level

✅ **Pass** — `TX →` and `RX ←` lines appear after restart without intervention  
❌ **Fail** — Level reverts to Warning after restart

---

## 7. Edge Cases

### 7.1 Unknown source routed externally

Route a source from the controller that is not in the CSV profile (Order number not present).

✅ **Pass** — Entity shows `Source N` fallback, no error in logs  
❌ **Fail** — Entity errors, shows blank, or integration disconnects

---

### 7.2 HA restart with active connection

Restart HA while the router is connected.

✅ **Pass** — Integration reconnects automatically, CSV names available immediately  
❌ **Fail** — Entities unavailable, or requires a manual reload

---

### 7.3 Multiple rapid HA restarts

Restart HA 3 times in quick succession. Check that orphaned TCP connections do not accumulate.

**Steps:**
1. Restart HA, wait 10 seconds
2. Restart again, wait 10 seconds
3. Restart a third time, wait for full startup
4. Check `connection #N` in logs — should be 3 or less

✅ **Pass** — `connection #` ≤ 3, controller accepts connection  
❌ **Fail** — Connection refused (too many open sockets on controller)

---

## Results Template

```
Date:
HA Version:
Integration Version:
Tester:

── Connection ──────────────────────────────────────────────────────
1.1 Initial connect:           PASS / FAIL
1.2 Route state on connect:    PASS / FAIL — controller responds to .I: YES / NO
1.3 Reconnect:                 PASS / FAIL

── Receiving Updates ───────────────────────────────────────────────
2.1 External route change:     PASS / FAIL
2.2 Rapid changes:             PASS / FAIL

── Routing from HA ─────────────────────────────────────────────────
3.1 Route via select:          PASS / FAIL
3.2 Optimistic update:         PASS / FAIL
3.3 .UV echo after HA take:    PASS / FAIL — controller echoes .UV: YES / NO
3.4 Route via service:         PASS / FAIL
3.5 Non-contiguous source:     PASS / FAIL

── CSV Profile ─────────────────────────────────────────────────────
4.1 Names on startup:          PASS / FAIL
4.2 Re-import CSV:             PASS / FAIL
4.3 Clear CSV:                 PASS / FAIL

── Lovelace Card ───────────────────────────────────────────────────
5.1 Card loads:                PASS / FAIL
5.2 Source list:               PASS / FAIL
5.3 Route via card:            PASS / FAIL
5.4 Favourites persist:        PASS / FAIL
5.5 Matrix view:               PASS / FAIL
5.6 Search:                    PASS / FAIL

── Diagnostics ─────────────────────────────────────────────────────
6.1 Download diagnostics:      PASS / FAIL
6.2 Log level persistence:     PASS / FAIL

── Edge Cases ──────────────────────────────────────────────────────
7.1 Unknown source:            PASS / FAIL
7.2 HA restart:                PASS / FAIL
7.3 Rapid restarts:            PASS / FAIL
```
