# 04 — Projects and schedules

**Goal:** the two surfaces a returning user hits on day two. Both were reshaped
recently, and both fail quietly.

**Preconditions:** task 03 finished. Keep using the same tab. Screenshots go to
`$TMPDIR/ciao-release-walk/<version>/` as `04-projects-<step>.png`.

Two separate things are checked here, and they must not be conflated:

1. **An app project with a scaffolded vault note.** Created from the sidebar's
   `+`, it lives in the sidebar and its page is `/project/<id>`. Creating it
   **scaffolds the project's vault note** and binds the project to it, so the
   new project is vault-backed: its page offers a **Complete** action and a
   ••• actions menu with *Delete project* (`web/src/components/ProjectView.vue`
   renders *Complete* and the ••• menu for a project with a `vault_folder`;
   auto-created projects are excluded from both branches).
2. **A vault project note.** A note in the memory vault under `projects/`. It is
   completed from **Memory → To decide → To revisit**
   (`/memory/review?show=revisit`), which posts `complete` to
   `/api/vault/review`. That is the flow that moves the note to
   `projects/completed/` and rewrites its `status:` to `completed`.

## Step 1 — create an app project with a scaffolded vault note (smoke)

Open the app root in the tab. The sidebar's `+` is the project entry point.
Create a project called `Release smoke vX.Y.Z`, then land on `/project/<id>`.

Run this only in an already isolated smoke workspace/engine. If none is
available, report this creation checkpoint **BLOCKED**; do not create a live
engine or seed a smoke workspace into the operator's workspace. **Use a fixture
name whose slug is not already bound in this workspace** —
append a timestamp or an attempt suffix (e.g. `Release smoke vX.Y.Z #2`), or
run this step in a fresh smoke workspace. The name you pick leaves the fixture
in the sidebar, so a second walkthrough of the same version would otherwise
hit the same slug: `_scaffold_project_note` refuses to bind a slug another
project (or a project already moved under `projects/completed/<slug>`)
   already holds, and the new project then
stays registry-only, with no vault folder. **A newly created project that is
not vault-backed is only a finding if its slug was free** — a taken slug is
the one legitimate reason, so check the slug before calling it.

**Watch for on create:** the project page opens, the sidebar row appears, and
nothing is left in a half-created state if you navigate away and back. The
page also says where the project lives: the **Lives in** rail names its vault
folder (the slug of the project name, e.g. `release-smoke-v1-6-0` for
`v1.6.0`), because creating the project scaffolded its vault note under the
workspace's `projects/active/`.

**Leave this project exactly as created.** Do **not** press Complete and do
**not** press Delete: this fixture is only created and observed, never closed.
The scaffolded vault note is part of the fixture and stays in place with the
project — that write is the creation working, not leakage into the workspace,
and it is not cleaned up afterwards. The page must offer a **Complete** action
and its ••• actions menu must offer *Delete project*. Both are rendered for a
non-auto project with a vault folder. (The sidebar row's own menu is a different
one and offers *Rename* + *Delete*, so scope this check to the project page.)
Record the project id, its vault folder, and the fact that it is untouched.

## Step 2 — complete an isolated synthetic vault project note

This is the vault half, and it must run against an **already isolated throwaway
smoke workspace/engine**, never the operator's real notes. If no isolated
smoke workspace/engine is available,
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
const res = await fetch(
  `/api/vault/review?workspace=${encodeURIComponent(ws)}`,
  { credentials: 'same-origin' },
);
if (!res.ok) {
  console.log('review fetch failed:', res.status, await res.text());
} else {
  const body = await res.json();
  console.log(body.candidates.filter(c =>
    c.path.endsWith('release-smoke-vX-Y-Z-<unique>.md')));
}
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
- The app project from Step 1 is **still in the sidebar**, untouched. Do
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

## Step 4 — the Webhook triggers section

On `/schedules`, the Webhook triggers section must be present, below the
schedule list. Read it only: its rows and the empty state should render with no
error. Do not create a trigger. If one is created to test the section, delete it
in the same step.

## Checkpoints

- `04-projects-01-created.png` — the app project page with its sidebar row
  (Step 1). Look for: page populated, row present, the vault-backed **Complete**
  action and ••• *Delete project* menu offered, nothing clipped. Leave it
  untouched.
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

- **App project (Step 1):** created from the sidebar, row present, vault-backed
  (its vault folder scaffolded and named in the **Lives in** rail), **Complete**
  and page's ••• *Delete project* menu offered, neither pressed, left untouched.
- **Vault project note (Step 2):** isolated workspace used (name it), synthetic
  seed path, `completable: true` confirmed, completion applied only to that row,
  source gone, completed note body retained with `status: completed`, row absent
  from To revisit, note present under the map's List. State **BLOCKED** here if
  no isolated workspace was available.
- `/automations` -> `/schedules` redirect landed correctly.
- Any schedule row with a wrong state or a link to nowhere.
