# Multi-Device Handover & Active-Standby Architecture Plan

Status: Proposed  
Target Repository: `raffaelefarinaro/ciaobot`  
Date: 2026-07-27  

---

## Executive Summary

When Ciaobot is deployed across multiple computers (e.g., an always-on **Home Server** and a mobile **Laptop**), running active backend processes on both machines concurrently creates operational hazards:
1. **Duplicate Automations & Schedules**: Cron-like background schedules (memory curation, workspace audit, dependency reviews, skill evolution) run independently on both machines, causing redundant execution and API token waste.
2. **Git Workspace Race Conditions**: Both backends commit and push dirty states (`memory-vault/`, `.runtime/schedules.json`) to `origin`, resulting in merge conflicts or overwritten state.
3. **Stale Session Context**: Vault edits or transcript archives created on the primary machine are not present on the secondary machine when switching working environments.

This plan specifies a **Multi-Device Handover System** based on an **Active-Standby Leader Lease** architecture. One instance acts as the **Active Leader** (executing schedules, loops, and background backup pushes), while secondary instances operate in **Standby Mode** (pausing background automations, maintaining read/write local chat accessibility, and standing ready to take over active control).

---

## Non-Negotiable Requirements

1. **Automation Isolation**: Background schedules, auto-start loops, and automatic background git pushes MUST run on the **Active Node** only.
2. **Single Vault & Repo Integrity**: Sync operations MUST ensure git synchronization before promoting a node from `standby` to `active`.
3. **Conflict Safety Net**: Handover git pulls MUST reuse Ciaobot's existing interactive merge chat (`MERGE_PROMPT`) to resolve conflicts dynamically.
4. **Graceful Standby Mode**: A Standby node MUST remain usable for manual user chats and local tool execution, surfacing clear UI indicators that automations are active elsewhere.
5. **Offline / Unreachable Peer Fallback**: If the active peer is offline or unreachable over the network, the user MUST be able to perform a **Force Takeover**.
6. **Zero-Secret Sync**: Credentials (`.env`, secrets, local SSH keys) and process-local files (`.runtime/server.lock`) MUST remain machine-local and never sync across peers.

---

## Architecture & Data Model

### 1. Node State File (`.runtime/node_state.json`)

Each Ciaobot backend instance manages its role and peer awareness in a local file at `.runtime/node_state.json`:

```json
{
  "node_id": "macbook-pro-raffaele",
  "role": "standby",
  "active_since": "2026-07-27T12:00:00Z",
  "last_handover": "2026-07-27T12:00:00Z",
  "peers": [
    {
      "node_id": "home-server",
      "url": "http://192.168.1.50:8543",
      "last_seen": "2026-07-27T13:30:00Z",
      "is_active": true
    }
  ]
}
```

### 2. Role Definitions

| Role | Schedulers & Cron | Background Loops | Git Backup Push | Manual PWA Chats | Local Tool Exec |
| --- | --- | --- | --- | --- | --- |
| **`active`** | Enabled | Enabled | Enabled | Enabled | Enabled |
| **`standby`** | Paused | Paused | Disabled | Enabled (with Standby banner) | Enabled |

### 3. Environment Variables & App Settings

- `CIAO_NODE_ID`: Unique node identifier (defaults to system hostname, e.g. `home-server`).
- `CIAO_DEFAULT_NODE_ROLE`: Startup default role if state file does not exist (`active` or `standby`). Defaults to `active` for standalone backwards compatibility.
- `CIAO_PEER_NODES`: Comma-separated list of peer base URLs (e.g. `http://home-server.local:8543,http://192.168.1.50:8543`).

---

## Handover Protocol & Workflow

### Standard Handover Sequence (Peer Reachable)

```
 [ User on Laptop ]          [ Laptop Backend ]         [ Home Server Backend ]         [ Git Remote ]
         │                           │                             │                         │
         ├─── Click "Take Over" ────►│                             │                         │
         │                           ├─── POST /api/node/demote ──►│                         │
         │                           │                             ├── Drain active turns    │
         │                           │                             ├── Commit pending work   │
         │                           │                             ├── Push branch ─────────►│
         │                           │                             ├── Set role = "standby"  │
         │                           │◄── HTTP 200 {demoted:true} ─┴─ (Schedulers paused)    │
         │                           │                                                       │
         │                           ├─── Fetch & Resync ───────────────────────────────────►│
         │                           │◄── Merge clean or launch conflict chat ───────────────┤
         │                           ├─── Set role = "active"                                │
         │                           ├─── Resume Schedulers & Loops                          │
         │   UI Updates to Active    │                                                       │
         │◄──────────────────────────┴───────────────────────────────────────────────────────┘
```

#### Step-by-Step Handover Flow:

1. **Handover Trigger**:
   - User clicks **"Take Over (Switch Active Control Here)"** in Settings → Handover or triggers the command palette option.
2. **Preflight & Demotion Signal**:
   - Laptop checks if the configured active peer is reachable over HTTP/HTTPS (e.g. `POST /api/node/demote`).
   - If reachable, the Active Peer (Home Server):
     a. Intercepts new background jobs and drains ongoing turns.
     b. Commits local pending changes via `commit_pending()`.
     c. Pushes working branch to `origin` via `push_branch()`.
     d. Writes `role = "standby"` into `.runtime/node_state.json`.
     e. Disables background schedule triggers (`ciao/schedules.py`) and loop iterations (`ciao/loops.py`).
     f. Responds to demote request with HTTP 200 OK.
3. **Repository Resync**:
   - Requesting node (Laptop) calls `sync_branch()` / `resync_branch()`.
   - Pulls latest state from `origin`.
   - **Conflict Handling**: If git conflict occurs during pull, Ciaobot's automatic merge chat is dispatched using `MERGE_PROMPT`, allowing Ciaobot's AI agent to cleanly merge vault markdown notes and schedule states.
4. **Promotion**:
   - Laptop sets its role to `"active"` in `.runtime/node_state.json`.
   - Schedulers and active loops are activated.
   - PWA UI broadcasts WebSocket state update: `node_role_changed { role: "active" }`.

---

### Force Takeover Sequence (Peer Unreachable / Offline)

If the home server is offline (power outage, disconnected network), the peer demotion HTTP request will time out:
1. PWA surfaces a modal: **"Active peer home-server is unreachable. Perform Force Takeover?"**
2. User confirms **"Force Takeover"**.
3. Laptop performs local commit `commit_pending()`.
4. Laptop fetches and merges `origin/<branch>` (using `resync_branch()`).
5. Laptop writes `role = "active"` into `.runtime/node_state.json`, promoting itself immediately.
6. When Home Server comes back online later, its initial preflight check detects that `origin/<branch>` contains a newer active lease / commit from Laptop, prompting Home Server to automatically step down to Standby mode.

---

## Code Components & Modifications

### 1. `ciao/node_state.py` [NEW]
Manager module for node state, peer registration, heartbeat pings, and role transitions.
- `NodeStateManager`:
  - `get_role() -> str` (`"active"` | `"standby"`)
  - `is_active() -> bool`
  - `set_role(role: str) -> None`
  - `demote() -> dict`
  - `handover(target_node_url: str | None, force: bool = False) -> dict`

### 2. `ciao/schedules.py` [MODIFY]
- Update `ScheduleManager` to verify `NodeStateManager.is_active()` before executing any scheduled job run.
- When `is_active()` is False, skip scheduled tick execution and record a log entry (`INFO: Skipping scheduled run because node role is standby`).

### 3. `ciao/loops.py` [MODIFY]
- Update `LoopManager` to check `NodeStateManager.is_active()` on every loop iteration tick.
- If in Standby mode, log and skip loop turn dispatch.

### 4. `ciao/local_session.py` [MODIFY]
- Guard background backup push loops so they only execute when `NodeStateManager.is_active()` is True.

### 5. `ciao/web/routes_api.py` [MODIFY]
Add new REST API endpoints for node management:
- `GET /api/node/status`: Returns current node metadata, role (`active`/`standby`), active peer info, and git status.
- `POST /api/node/handover`: Initiates handover to current machine (optional body `{ "force": bool }`).
- `POST /api/node/demote`: Peer endpoint called during handover to demote the recipient node.
- `POST /api/node/peers`: Add or remove peer URLs.

### 6. `web/src/components/SettingsView.vue` & `NodeHandoverPanel.vue` [NEW/MODIFY]
Add **Handover** section under Settings:
- Status banner indicating active leader status.
- Active vs Standby badge.
- **Switch to This Computer** button.
- **Force Takeover** confirmation modal.
- Peer configuration list with live ping latency indicators.

---

## PWA User Interface Design

### Standby Mode Banner (Top of App Header)
```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│ ℹ️ Standby Mode — Automations are running on home-server. [Take Over Control Here]     │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

### Settings → Handover Panel
```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│  Node & Handover Management                                                            │
│                                                                                        │
│  Current Device: macbook-pro-raffaele                                                  │
│  Status: 🟡 STANDBY MODE                                                               │
│                                                                                        │
│  Active Leader Node: 🟢 home-server (http://192.168.1.50:8543)                         │
│  Last Sync: 2026-07-27 13:15:00 UTC                                                    │
│                                                                                        │
│  [ Switch Active Control to This Computer ]    [ Force Takeover (Server Offline) ]     │
│                                                                                        │
│  Registered Peers:                                                                     │
│  • home-server (http://192.168.1.50:8543) — Online (ping 12ms) [Active]                │
│  • macbook-pro-raffaele (Local) — Online [Standby]                                     │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## Verification & Test Plan

### Automated Tests
1. `pytest tests/test_node_state.py`:
   - Unit tests for role persistence, default behavior, promotion, and demotion hooks.
2. `pytest tests/test_schedules_node_role.py`:
   - Verify schedule manager skips dispatch when node role is `standby`.
3. `pytest tests/test_loops_node_role.py`:
   - Verify loop iterations pause when node role is `standby`.
4. `pytest tests/test_handover_api.py`:
   - Test `/api/node/status`, `/api/node/handover`, and `/api/node/demote` route responses and error envelopes.

### Manual Verification Workflow
1. Start instance A on Port 8543 (`CIAO_NODE_ID=home-server`).
2. Start instance B on Port 8544 (`CIAO_NODE_ID=laptop`).
3. Verify instance A is `active` and instance B is `standby`.
4. Trigger handover from instance B via API or PWA.
5. Verify instance A commits work, pushes to git, and switches to `standby`.
6. Verify instance B pulls from git, resyncs, switches to `active`, and enables schedulers.

---

## Rollout Roadmap

- **Phase 1**: Node state module (`ciao/node_state.py`) and schedule/loop role guarding.
- **Phase 2**: Handover REST API endpoints (`/api/node/status`, `/api/node/handover`, `/api/node/demote`) and git resync hooks.
- **Phase 3**: Vue PWA UI panel in Settings, header banner, and WebSocket event integration.
