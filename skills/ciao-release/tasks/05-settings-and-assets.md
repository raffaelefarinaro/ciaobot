# 05 — Settings, and the asset lists

**Goal:** Settings is where four lists were recently re-scoped to the selected
workspace. The reads and the writes were changed together on purpose, so this
is where a partial change shows up.

**Preconditions:** task 04 finished. Keep using the same tab. Screenshots go to
`$TMPDIR/ciao-release-walk/<version>/` as `05-settings-<step>.png`.

## Step 1 — walk every Settings route

Settings is **not a single page that scrolls**. Each section has its own route,
listed in the sidebar, and each renders only its own cards:

`/settings` (General, the default), `/settings/workspaces`, `/settings/models`,
`/settings/skills`, `/settings/subagents`, `/settings/commands`,
`/settings/mcp`.

Visit each route in turn. On each one, **wait for that route's own heading or
load state to resolve** — do not sleep a fixed interval and call it loaded.
`/settings/providers` is a redirect to `/settings/models#chat-providers`; it is
not a section of its own.

`/settings` (General) carries What can Ciaobot do?, Appearance, This host,
memory backup, insights, Updates, the main workspace, workspace health, the PWA
password card, Other devices, app install, notifications, keyboard shortcuts,
open source (and Debug only in dev mode). Watch for:

- The online backup card shows state and last successful upload; scope,
  repository and raw diagnostics sit behind a native `<details>` disclosure.
- The "What can Ciaobot do?" card at the top links out to the public
  feature guide.
- The critique panel on **Models** is *explained*, not just labelled — what a
  panel is, that each model reviews independently, that `/critique` or the skill
  invokes it. A bare "Models asked for an adversarial review" is the pre-fix
  state.

The **"On this page" rail is route-specific**: it is built from the cards the
current tab rendered (`tocItems` in `web/src/components/SettingsView.vue`), so
it lists the sections of *that* route, not the seven Settings sections on one
scroll. A rail that names another route's cards is a finding.

**Every section must load without hanging.** A section that never resolves is a
finding; do not wait on it more than once.

## Step 2 — the four asset lists are scoped to one workspace

This is the point of the task. **Skills, Subagents, Commands and MCP servers all
belong to the workspace selected in the sidebar.** Only one workspace is
selected at a time, so: record the four lists on the current workspace, switch
the sidebar to another workspace, let the active-workspace watcher refetch, and
record them again. On a single-workspace install there is nothing to switch to:
report the before/after checkpoint **BLOCKED** rather than inventing a second
workspace.

Record the names visible in the four lists (for example by reading the text of
the skill-name and row elements in the page).

**Watch for, precisely:**

- All four refetch together on the switch. Scoping only the reads would be worse
  than the original bug: a create would file an asset in a root the list never
  shows.
- **Stock skills stay in every list.** `sync-skills` installs them into every
  agent root, so a scoped list still shows the built-ins. Their absence is a bug,
  not the scoping working.
- **Global rows may legitimately be identical across workspaces.** Subagents and
  commands with `scope: global` come from the operator's own `~/.claude`
  directory, which every workspace's provider sees
  (`ciao/web/agent_assets.py`: `list_subagents` / `list_command_assets`). Expect
  the same Global subagents on both workspaces; do **not** require the two
  workspaces' inventories to differ in count or names.

## Step 3 — a read-only consistency check against the scoped API

Compare each workspace-specific list to the scoped API **in the same browser
session** (same origin, same session cookie), and name the endpoints the app
actually calls (`web/src/components/SettingsView.vue`, `useMcpServers.ts`):

- Skills — `/api/admin/skills?workspace=<name>`
- Subagents — `/api/agent-assets?workspace=<name>` (`subagents`)
- Commands — `/api/agent-assets?workspace=<name>` (`commands`)
- MCP servers — `/api/mcp/status?workspace=<name>`

Run the fetch inside the authenticated browser tab, where the session cookie
lives:

```js
const ws = '<workspace>';
for (const path of [
  `/api/admin/skills?workspace=${encodeURIComponent(ws)}`,
  `/api/agent-assets?workspace=${encodeURIComponent(ws)}`,
  `/api/mcp/status?workspace=${encodeURIComponent(ws)}`,
]) {
  const res = await fetch(path, { credentials: 'same-origin' });
  console.log(path, res.status, await res.json());
}
```

Do not read `PWA_AUTH_TOKEN`, do not copy cookies to a shell, and do not read
the password. A bare `curl` from the shell is **not** evidence the endpoint
works: no session cookie means a `401`, and a `401` only proves the request was
rejected as unauthenticated — it says nothing about whether the route resolves a
workspace. A `500` is a failure; a `401` is neither a pass nor a failure.

Do not create, edit or delete anything in Settings.

## Checkpoints

- `05-settings-01-general.png` — `/settings` (General). Look for: its own
  sections present, rail listing this route's cards, backup card collapsed
  details, no clipped headings, no overlap.
- `05-settings-02-route.png` — one additional route (e.g. `/settings/models` or
  `/settings/skills`). Look for: only that route's cards, its own rail where the
  route has more than one section (`/settings/mcp` legitimately shows none),
  load state resolved, no other route's sections bleeding in.
- `05-settings-03-assets-before.png` — the four asset lists on the first
  workspace. Look for: stock skills present, lists populated or a sensible
  empty state.
- `05-settings-04-assets-after.png` — the same lists after the workspace switch.
  Look for: all four refetched together, stock skills still present, Global rows
  allowed to be unchanged, no stale rows, no stray toast.

Redact credentials and account details from any screenshot.

## Verdict

Fill one: **pass** / **finding** / **blocked**.

Record:

- Which Settings routes loaded, which hung.
- Whether the "On this page" rail matched the route it was on.
- Before/after asset lists across a workspace switch, and whether all four moved
  together (Global rows explicitly allowed to stay the same).
- Whether stock skills were still present in the scoped list.
- Any copy that describes a whole-install inventory when the page is scoped.

This is the last task. Leave the tab open for the operator to look at.
