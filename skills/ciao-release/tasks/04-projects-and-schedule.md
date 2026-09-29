# 04 — Projects and schedules

**Goal:** the two surfaces a returning user hits on day two. Both were reshaped
recently, and both fail quietly.

**Preconditions:** task 03 finished. Still the same TaskSpace.

## Round 1 — create a project, then complete it

```js
const task = await taskSpace("ciaobot release walkthrough");
const page = task.page("p1");
await page.goto("http://127.0.0.1:8443/");
await page.waitForLoadState("domcontentloaded");
console.log(await page.snapshot());
```

The sidebar's `+` is the project entry point. Create a project called
`Release smoke vX.Y.Z`, then land on `/project/<id>`.

**Watch for on create:** the project page opens, the sidebar row appears, and
nothing is left in a half-created state if you navigate away and back.

**Watch for on complete:** a project marked **Complete** leaves the sidebar, and
its notes stay in memory. That second half is the part that breaks — the row
vanishing while the note went with it is a data-loss finding, not a cosmetic
one. Verify the note survives:

```js
await page.goto("http://127.0.0.1:8443/memory/map");
await page.waitForLoadState("domcontentloaded");
console.log(await page.snapshot());
```

Leave the project completed. Do not delete it — the operator may want to see it.

## Round 2 — the schedules page, both addresses

`/automations` is a **redirect** to `/schedules`, not a second page
(`web/src/router.ts`). A release that broke the redirect would show a blank
route with no error, so check the address bar, not just the content:

```js
const task = await taskSpace("ciaobot release walkthrough");
const page = task.page("p1");
await page.goto("http://127.0.0.1:8443/automations");
await page.waitForLoadState("domcontentloaded");
console.log({ landedOn: await page.url() });
console.log(await page.snapshot());
```

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
- "Needs a look" rows link to the **run chat**, not to the automations page.

Report any row whose label and link disagree.

## Report

- Project created, completed, sidebar cleared, note still in memory.
- `/automations` → `/schedules` redirect landed correctly.
- Any schedule row with a wrong state or a link to nowhere.

Do not create, edit or delete a real schedule. Reading rows is the whole task.
