# 01 — Boot, unlock, first paint

**Goal:** prove the freshly installed engine actually serves a working PWA, and
get the session unlocked so tasks 02–05 run unattended.

**Preconditions:** `/ciao-dev-install` has just finished. The engine is live on
the operator's real workspace. The operator is at the machine.

Screenshots go to `$TMPDIR/ciao-release-walk/<version>/`, named
`NN-<task>-<step>.png`.

## Step 1 — open the app, stop at the door

Open the app at **the origin this install actually serves** — the URL the
`/ciao-dev-install` skill printed, or the address the installed service is bound to —
not a hard-coded `127.0.0.1:8443`. `PWA_HOST` defaults to `0.0.0.0` and
`PWA_PORT` to `8443` (`INTEGRATIONS.md`), but the operator may have changed
either, so read the running config rather than assume. Wait for the document to
load, then note the final URL and the page title.

**Then stop and ask the operator to type the dashboard password into the visible
browser window.** Do not read `PWA_AUTH_TOKEN` from the workspace `.env` and do
not type it yourself — that puts the password in this transcript for no benefit.
The `LoginView` is at `web/src/components/LoginView.vue`; the field is
`autocomplete="current-password"`.

If the page is *not* a login form, do **not** conclude the install has no door.
The browser may simply be reusing an already-signed-in session: once a session
cookie is present the route guard sends you straight to the app, and an
authenticated session is not proof that authentication is off. Tell the two
apart from the app itself — the PWA password card on Settings → General reports
`auth_required` (`GET /api/auth/settings`). Only `auth_required: false` means
there is genuinely no password.

## Step 2 — the app loads

After the operator has unlocked, wait up to ~20s for the sidebar or main
navigation to appear, then take the first-paint screenshot.

**What must be true:**

- No `EngineOfflineView` and no `StartupView` spinner still running — those mean
  the engine is not answering, which is a real failure, not a slow start. Wait
  up to ~20s before calling it.
- The sidebar renders: workspaces, projects, and the "what needs you" lanes.

**The device setup card is device state, not engine state.** `HomeSetupCard` is
driven entirely by the browser: it hides when this window is an installed app
(`isStandalone`), when notifications are already on or blocked, or after a
dismissal stored in this browser (`ciao-setup-card-dismissed`). It does **not**
auto-hide just because the engine is configured, so a card on screen is not by
itself a regression. The card's own state decides: `visible` is
`touched || !complete`, so a card whose steps both read done legitimately stays
on screen until it is touched in this session, and only then does a 2.5s timer
hide it. Look at the card's own state before deciding: report the self-hiding
failure only when both steps read done (installed, and notifications on/blocked)
**and the card has been interacted with in this session** yet it is still
showing after a moment; a card on screen before any interaction with both steps
done is correct, and a genuinely unfinished step is correct too. Read the
browser's state, do not assume the configured engine should have hidden it.

**The package-update tile is conditional on a newer *published* release.**
`HousekeepingStrip` shows it only when `/api/package/status` reports
`update_available: true`, which is `latest_version` newer than the installed
version. During a candidate walkthrough the candidate is typically *ahead* of
the latest published release, so `update_available` is false and the tile is
correctly absent — its absence is a pass, not a regression. Record the evidence
through an authenticated fetch inside the browser tab (the session cookie lives
there), never by reading `PWA_AUTH_TOKEN`:

```js
const res = await fetch('/api/package/status', { credentials: 'same-origin' });
console.log(res.status, await res.json());  // current_version, latest_version, update_available
```

A tile is expected iff that payload says `update_available: true`; when it does,
its lead button reads **"Update in Settings"** and release notes are the
secondary link. A `500` is a failure; a `401` only means the browser session is
not signed in.

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
  cards, no clipped text, no stray toast. For the setup card: absent, or present
  with a genuinely unfinished step. For the update tile: present only if
  `/api/package/status` reported `update_available: true`, with "Update in
  Settings" leading; absent is correct when the candidate is not behind the
  latest published release.

## Verdict

Fill one: **pass** / **finding** / **blocked**.

Record:

- Engine answered, and the served asset hash matches disk.
- Whether the door was a login form or a reused session (with `auth_required`
  from `/api/auth/settings` as the tie-breaker).
- `/api/package/status`: `current_version`, `latest_version`,
  `update_available`, and whether the tile matched it.
- Anything that looked wrong, wrong at this width, or slow to settle, with the
  checkpoint it shows up in.

Keep using the same tab for task 02.
