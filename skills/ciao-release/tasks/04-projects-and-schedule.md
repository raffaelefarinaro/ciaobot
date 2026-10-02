# 04 — Projects and schedules

**Goal:** the two surfaces a returning user hits on day two. Both were reshaped
recently, and both fail quietly.

**Preconditions:** task 03 finished. Keep using the same tab. Screenshots go to
`$TMPDIR/ciao-release-walk/<version>/` as `04-projects-<step>.png`.

Two separate things are checked here, and they must not be conflated:

1. **A plain app project.** Created from the sidebar's `+`, it lives in the
   sidebar and its page is `/project/<id>`. A project without a `vault_folder`
   has **no `Complete` action**: its actions menu offers only *Delete project*
   (`web/src/components/ProjectView.vue`). There is nothing to complete on it.
2. **A vault project note.** A note in the memory vault under `projects/`. It is
   completed from **Memory → To decide → To revisit**
   (`/memory/review?show=revisit`), which posts `complete` to
   `/api/vault/review`. That is the flow that moves the note to
   `projects/completed/` and rewrites its `status:` to `completed`.

## Step 1 — create a plain app project (smoke)

Open the app root in the tab. The sidebar's `+` is the project entry point.
Create a project called `Release smoke vX.Y.Z`, then land on `/project/<id>`.

**Watch for on create:** the project page opens, the sidebar row appears, and
nothing is left in a half-created state if you navigate away and back.

**Leave this project exactly as created.** Do not press Complete (it is not
offered) and do not Delete it. Its actions menu should show only *Delete
project*; a *Complete* action here would mean the app has started treating a
plain project as a vault entry, which is the confusion this task exists to
avoid. Record the project id and the fact that it is untouched.

## Step 2 — complete an isolated synthetic vault project note

This is the vault half, and it must run against a **throwaway smoke workspace**,
never the operator's real notes. If the walkthrough is running against the
operator's real workspace and no isolated smoke workspace/engine is available,
**mark this completion checkpoint BLOCKED** and say so plainly — do not claim it
passed and do not seed into the real vault.

**Seed.** In the isolated workspace, resolve the vault root and write one
synthetic flat project note:

- Candidate path: `memory-vault/projects/release-smoke-vX-Y-Z-<unique>.md`
  (on disk, `<vault root>/projects/release-smoke-vX-Y-Z-<unique>.md`).
- Frontmatter: `type: project`, `status: active`, a `title:`, and
  `created: 2020-01-01` / `updated: 2020-01-01`.
- Body: unmistakable synthetic smoke text, e.g.
  `Release smoke fixture for vX.Y.Z — safe to complete and leave; not a real project.`
- No other note links to it, so it is an orphan review candidate by design. That
  is intentional and is part of the fixture, not a defect.

Refuse to seed if either the source path or its completed counterpart
(`projects/completed/release-smoke-vX-Y-Z-<unique>.md`) already exists, or if the
resolved vault is the operator's real one.

**Gate before acting.** In the authenticated app tab, confirm the queue lists
this exact candidate and marks it completable, before pressing anything:

```js
const ws = '<smoke-workspace>';
const body = await (await fetch(
  `/api/vault/review?workspace=${encodeURIComponent(ws)}`
)).json();
console.log(body.candidates.filter(c =>
  c.path.endsWith('release-smoke-vX-Y-Z-<unique>.md')));
```

The matching candidate must carry `completable: true` (it comes from the same
helpers `complete_project_note` gates on). If it is missing, or `completable` is
false, stop and report a finding — do not press Complete.

**Complete.** Open `/memory/review?show=revisit`. Find the row whose visible
path/title is the synthetic note, confirm its displayed identity, and press
**Complete** on **that row only** (confirm the dialog). No blanket queue
actions, and do not complete any other row.

**Verify retention.**

- The source path is gone from the vault.
- `memory-vault/projects/completed/release-smoke-vX-Y-Z-<unique>.md` exists, its
  synthetic body is retained verbatim, and its `status:` reads `completed`.
- The row is absent from `/memory/review?show=revisit`.
- The note is still visible under `/memory/map` → **List**.
- The plain app project from Step 1 is **still in the sidebar**, untouched. Do
  **not** assert that any unrelated sidebar row disappeared: completing a vault
  note moves the note, not the app project.

**Leave the completed synthetic note in place** for the operator to inspect. No
restore, no delete, no cleanup — none of it, and nothing at all in the real
workspace.

## Step 3 — the schedules page, both addresses

`/automations` is a **redirect** to `/schedules`, not a second page
(`web/src/router.ts`). A release that broke the redirect would show a blank
route with no error, so check the address bar, not just the content. Open
`/automations` in the tab, wait for it to load, and note the final URL.

**Must be true:** the final URL is `/schedules`, and the schedule list renders.

Then read the rows. The failure modes that matter here are all in the *state* of
a row, not in whether the page loaded:

- No row for a run whose chat was archived should still point at that chat.
  Archiving a run's chat now clears the entry's `skipped` flag to `ok` and
  publishes `schedules_changed` — before, a weekly entry pointed at an archived
  chat for a week.
- A run whose background subagents never settled is stamped **"unfinished"** on
  the row, not "skipped". If it says "skipped", the run has no question to
  answer, and the "last run needs you" link is pointing at an empty chat.
- "Needs a look" rows link to the **run chat**, not to the automations view.

Report any row whose label and link disagree.

Do not create, edit or delete a real schedule. Reading rows is the whole task.

## Checkpoints

- `04-projects-01-created.png` — the plain app project page with its sidebar
  row (Step 1). Look for: page populated, row present, the actions menu offering
  only *Delete project*, nothing clipped. Leave it untouched.
- `04-projects-02-completed.png` — after completing the **synthetic vault note**
  (Step 2), or the BLOCKED state if there is no isolated workspace. Look for:
  the completed note's row gone from To revisit, no error card, no stray toast.
- `04-projects-03-retained.png` — the retained note. Look for: the synthetic
  body still present under `projects/completed/`, `status: completed`, and the
  note visible under `/memory/map` → List.
- `04-projects-04-schedules.png` — `/schedules` after the `/automations`
  redirect (Step 3). Look for: URL in the address bar, rows with state labels
  that match their links, no blank route.

## Verdict

Fill one: **pass** / **finding** / **blocked**.

Record the two halves separately:

- **App project (Step 1):** created from the sidebar, row present, only *Delete
  project* offered, left untouched.
- **Vault project note (Step 2):** isolated workspace used (name it), synthetic
  seed path, `completable: true` confirmed, completion applied only to that row,
  source gone, completed note body retained with `status: completed`, row absent
  from To revisit, note present under the map's List. State **BLOCKED** here if
  no isolated workspace was available.
- `/automations` -> `/schedules` redirect landed correctly.
- Any schedule row with a wrong state or a link to nowhere.
