# 01 — Boot, unlock, first paint

**Goal:** prove the freshly installed engine actually serves a working PWA, and
get the session unlocked so tasks 02–05 run unattended.

**Preconditions:** `/ciao-dev-install` has just finished. The engine is live on
the operator's real workspace. The operator is at the machine.

Screenshots go to `$TMPDIR/ciao-release-walk/<version>/`, named
`NN-<task>-<step>.png`.

## Step 1 — open the app, stop at the door

Open `http://127.0.0.1:8443/` in a tab and wait for the document to load. Note
the final URL and the page title.

**Then stop and ask the operator to type the dashboard password into the visible
browser window.** Do not read `PWA_AUTH_TOKEN` from the workspace `.env` and do
not type it yourself — that puts the password in this transcript for no benefit.
The `LoginView` is at `web/src/components/LoginView.vue`; the field is
`autocomplete="current-password"`.

If the page is *not* a login form, say so and skip the unlock — an install with
`PWA_AUTH_REQUIRED=false` has no door.

## Step 2 — the app loads

After the operator has unlocked, wait up to ~20s for the sidebar or main
navigation to appear, then take the first-paint screenshot.

**What must be true:**

- No `EngineOfflineView` and no `StartupView` spinner still running — those mean
  the engine is not answering, which is a real failure, not a slow start. Wait
  up to ~20s before calling it.
- The sidebar renders: workspaces, projects, and the "what needs you" lanes.
- The **device setup card is gone** if setup is already done. It is meant to
  auto-hide (`HomeSetupCard`) — a card still showing for a configured install is
  a regression worth reporting.
- `HousekeepingStrip` shows the package-update tile with an **"Update in
  Settings"** button leading, and release notes as the secondary link.

Read the whole page once here, where you are establishing what the app looks
like; afterwards read only what is visible.

## Step 3 — is the served build the build we cut?

The engine can be serving a stale bundle. Compare the asset hash the *served*
shell names against the one on disk. Read the served names from the page by
evaluating JavaScript in it: collect the `src` of every `script[src]` and the
`href` of every `link[href]`, and keep those containing `/assets/`. Then, from
the repo root:

```bash
grep -oE 'assets/index-[A-Za-z0-9_-]+\.js' ciao/web/static/index.html
```

They must match. A mismatch means `npm run build` ran before the last source
change, or the install copied older package data — rebuild, reinstall, restart.

## Checkpoints

- `01-boot-01-door.png` — the login form (or the app, if there is no door).
  Look for: a sane centred form, no clipped field, no error banner.
- `01-boot-02-home.png` — first paint after unlock. Look for: sidebar present
  and not collapsed at this width, no spinner or offline view, no overlapping
  cards, no clipped text, no stray toast, setup card absent,
  `HousekeepingStrip` with "Update in Settings" leading.

## Verdict

Fill one: **pass** / **finding** / **blocked**.

Record:

- Engine answered, and the served asset hash matches disk.
- Anything that looked wrong, wrong at this width, or slow to settle, with the
  checkpoint it shows up in.

Keep using the same tab for task 02.
