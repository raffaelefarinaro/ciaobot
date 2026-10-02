# 05 — Settings, and the asset lists

**Goal:** Settings is the one page where four lists were recently re-scoped to
the selected workspace. The reads and the writes were changed together on
purpose, so this is where a partial change shows up.

**Preconditions:** task 04 finished. Keep using the same tab. Screenshots go to
`$TMPDIR/ciao-release-walk/<version>/` as `05-settings-<step>.png`.

## Step 1 — walk the page

Settings is one long scrolling page with an "On this page" rail, not a tab bar.
Open `/settings`, give it about 1.5 seconds, and walk it top to bottom, taking a
full-page screenshot once.

**Every section must load without hanging:** General, Workspaces, Models &
providers, Skills, Subagents, Commands, MCP servers. A section that never
resolves is a finding; do not wait on it more than once.

Watch for:

- The online backup card shows state and last successful upload; scope,
  repository and raw diagnostics sit behind a native `<details>` disclosure.
- The "What can Ciaobot do?" section at the top of General links out to the
  public feature guide.
- The critique panel is *explained*, not just labelled — what a panel is, that
  each model reviews independently, that `/critique` or the skill invokes it.
  A bare "Models asked for an adversarial review" is the pre-fix state.

## Step 2 — the four asset lists are scoped to one workspace

This is the point of the task. **Skills, Subagents, Commands and MCP servers all
belong to the workspace selected in the sidebar.** Only one workspace is
selected, so: switch the sidebar to another workspace, and all four lists must
change together.

Record the names visible in the four lists (for example by reading the text of
the skill-name and row elements in the page), switch the workspace in the
sidebar, let the active-workspace watcher refetch, and record them again. The
lists must now describe the *other* workspace's agent root.

**Watch for, precisely:**

- All four change together. Scoping only the reads would be worse than the
  original bug: a create would file an asset in a root the list never shows.
- **Stock skills stay in every list.** `sync-skills` installs them into every
  agent root, so a scoped list still shows the built-ins. Their absence is a bug,
  not the scoping working.
- The copy does not imply the tabs are workspace-filtered when they are not. The
  distinction is real: `/api/admin/skills` merges every agent root,
  `/api/agent-assets` reads the primary root plus `~/.claude`, and the MCP list
  is the primary root's `.mcp.json`. If the page describes a whole-install
  inventory, that is the copy regression.

## Step 3 — a read-only consistency check

Do not create, edit or delete anything in Settings. Confirm instead that what
the page shows is what the API says:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8443/api/agent-assets
curl -s http://127.0.0.1:8443/api/admin/skills | head -c 400
```

A 401 here is a pass, not a failure — the session cookie lives in the browser,
not in your shell. A 500 is not.

## Checkpoints

- `05-settings-01-full.png` — the whole view. Look for: every section present,
  rail visible, backup card collapsed details, no clipped headings, no overlap.
- `05-settings-02-assets-before.png` — the asset lists on the first workspace.
  Look for: stock skills present, lists populated or a sensible empty state.
- `05-settings-03-assets-after.png` — the same lists after the workspace
  switch. Look for: all four changed, stock skills still present, no stale
  rows, no stray toast.

## Verdict

Fill one: **pass** / **finding** / **blocked**.

Record:

- Which Settings sections loaded, which hung.
- Before/after asset lists across a workspace switch, and whether all four
  moved together.
- Whether stock skills were still present in the scoped list.
- Any copy that describes a whole-install inventory.

This is the last task. Leave the tab open for the operator to look at.
