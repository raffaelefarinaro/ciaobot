# 04 — Projects and schedules

**Goal:** the two surfaces a returning user hits on day two. Both were reshaped
recently, and both fail quietly.

**Preconditions:** task 03 finished. Keep using the same tab. Screenshots go to
`$TMPDIR/ciao-release-walk/<version>/` as `04-projects-<step>.png`.

## Step 1 — create a project, then complete it

Open the app root in the tab. The sidebar's `+` is the project entry point.
Create a project called `Release smoke vX.Y.Z`, then land on `/project/<id>`.

**Watch for on create:** the project page opens, the sidebar row appears, and
nothing is left in a half-created state if you navigate away and back.

**Watch for on complete:** a project marked **Complete** leaves the sidebar, and
its notes stay in memory. That second half is the part that breaks — the row
vanishing while the note went with it is a data-loss finding, not a cosmetic
one. Verify the note survives by opening `/memory/map` and looking for it.

Leave the project completed. Do not delete it — the operator may want to see it.

## Step 2 — the schedules page, both addresses

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

- `04-projects-01-created.png` — the new project page with its sidebar row.
  Look for: page populated, row present, nothing clipped.
- `04-projects-02-completed.png` — after Complete. Look for: row gone from the
  sidebar, no error card, no stray toast.
- `04-projects-03-memory-map.png` — `/memory/map`. Look for: the project's note
  still present, map not empty or overlapping.
- `04-projects-04-schedules.png` — `/schedules` after the redirect. Look for:
  URL in the address bar, rows with state labels that match their links, no
  blank route.

## Verdict

Fill one: **pass** / **finding** / **blocked**.

Record:

- Project created, completed, sidebar cleared, note still in memory.
- `/automations` -> `/schedules` redirect landed correctly.
- Any schedule row with a wrong state or a link to nowhere.
