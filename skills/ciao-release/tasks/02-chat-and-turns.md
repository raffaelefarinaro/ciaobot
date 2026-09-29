# 02 — A chat, start to finish

**Goal:** the core loop. Start a chat, send real turns, watch them stream, stop
one. This is the path a user spends their day in.

**Preconditions:** task 01 finished; the session is unlocked. `spaceId` in hand.

Everything below is one TaskSpace, resumed each round. Task spaces, tabs and page
labels persist between invocations; JavaScript variables do not.

Use a chat title that is obviously a release smoke test, e.g.
`Release smoke vX.Y.Z`. It makes the leftover obvious.

## Round 1 — start a chat

```js
const task = await taskSpace("ciaobot release walkthrough");
const page = task.page("p1");
await page.goto("http://127.0.0.1:8443/");
await page.waitForLoadState("domcontentloaded");
console.log(await page.snapshot());
```

From the snapshot, find the composer and the new-chat control
(`aria-label="Start a new chat"` on `HomeIntake`/`PaneHeader`). Prefer
accessibility roles and text over coordinates:

```js
// once you have the ref from the snapshot
await page.click("@<composer>");
await page.fill("@<composer>", "Reply with exactly: ok");
await page.keyboard.press("Enter");
await page.waitForURL(/.*\/chat\/.+/, { timeout: 15000 });
console.log({ url: await page.url() });
```

**Watch for:** the project picker. If the composer already names a project, the
chat must start directly — asking *which* project again is the bug #724-era
behaviour came from, and skipping it is the current intent. A picker appearing
here is a finding.

## Round 2 — the turn streams

```js
const task = await taskSpace("ciaobot release walkthrough");
const page = task.page("p1");
await page.waitForSelector("text=ok", { timeout: 120000 });
const shot = await page.screenshot({ path: "/tmp/ciao-02-turn.png" });
console.log({ shot, url: await page.url() });
console.log(await page.snapshot());
```

**Watch for, in order of how badly it matters:**

- The turn actually completes and the reply renders. An empty bubble, a spinner
  that never resolves, or a turn that ends in an error card is a **blocking
  finding** — do not continue past it; report and stop.
- **No toast fires for a normal turn.** A toast on a plain reply means a
  background or memory pass is surfacing as if the user asked for it.
- The transcript keeps its scroll position at the bottom, and does not jump
  mid-stream.

## Round 3 — a second turn, then stop it

Context continuity is the thing worth proving: the second turn should behave
like a conversation, not a fresh session.

```js
const task = await taskSpace("ciaobot release walkthrough");
const page = task.page("p1");
await page.fill("loc=css:textarea", "Now reply with exactly: still here");
await page.keyboard.press("Enter");
await page.waitForTimeout(3000);
// find the stop control in the snapshot and press it
console.log(await page.snapshot());
```

Then press the stop control mid-stream and confirm:

- the turn ends promptly rather than running to completion,
- the partial reply stays in the transcript (it is not discarded),
- the composer is usable again immediately.

**Report** each of the three separately. A stop button that leaves the composer
dead is a different bug from one that ignores the click, and the report should
not collapse them.

## Report

- Did the turn complete, in how long?
- Any toast, scroll jump, stuck spinner or error card.
- Stop behaviour, stated per the three points above.
- The chat URL, so the operator can open it.

Leave this chat open. Task 03 archives it.
