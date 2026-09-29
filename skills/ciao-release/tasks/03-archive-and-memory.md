# 03 — Archive, and the archived chat

**Goal:** archiving is the quiet end of the loop, and it quietly starts two
things — a memory pass and an index write. Both are easy to break and invisible
until they are.

**Preconditions:** task 02 left a chat open at `/chat/<id>`.

## Round 1 — archive it

```js
const task = await taskSpace("ciaobot release walkthrough");
const page = task.page("p1");
const before = await page.url();
console.log({ before });
console.log(await page.snapshot());
```

The archive control is the header's one labelled action
(`ChatPanel.vue`: `aria-label` + a visible `Archive` word — it gets words, not
just a glyph, so it is findable by role and name). Click it, then confirm the
dialog (`ConfirmDialog`, confirm label `Archive`):

```js
await page.click("loc=role:button[name*='Archive']");
await page.waitForSelector("loc=role:dialog", { timeout: 5000 });
await page.click("loc=role:button[name='Archive']");
await page.waitForTimeout(2000);
console.log({ after: await page.url() });
console.log(await page.snapshot());
```

**Watch for:**

- The pane clears optimistically and lands somewhere sensible (Home, or the
  next chat) — not a blank route.
- **No toast.** Archiving spawns a memory pass; that pass runs as an ordinary
  chat, and its `chat_result_ready` event used to raise a "Memory pass · …"
  toast. The event itself still publishes — only the toast is guarded. A toast
  here is a known regression shape and worth reporting precisely.
- No error card.

## Round 2 — reopen the archived chat

This is the #619 failure, and it is why the route exists. Navigate **directly**,
by URL, so the boot URL restore, the route watcher and the store all have to
agree:

```js
const task = await taskSpace("ciaobot release walkthrough");
const page = task.page("p1");
await page.goto("<the chat URL from task 02>");
await page.waitForLoadState("domcontentloaded");
await page.waitForTimeout(2500);
const shot = await page.screenshot({ path: "/tmp/ciao-03-archived.png" });
console.log({ shot });
console.log(await page.snapshot());
```

**Must be true:** the transcript renders — not an empty pane. #619 produced
`/chat/<archived-id>` resolving to *no panel at all*, so the archived footer was
unreachable. An empty pane with a working URL is exactly that bug.

**Watch for:**

- The archived footer is present: a link to the memory pass, and "Continue in
  new chat".
- **The page is inert** — no socket dial, no spinner, no "reconnecting". A
  read-only transcript that opens a live chat socket is a finding. To observe it
  from the client, install a WebSocket recorder before the app's first script
  runs, and assert no `/ws/chat/...` appears:
  ```js
  const task = await taskSpace("ciaobot release walkthrough");
  const page = task.page("p1");
  await page.addInitScript(() => {
    const seen = [];
    window.__wsUrls = seen;
    const Native = window.WebSocket;
    window.WebSocket = class extends Native {
      constructor(url, protocols) { seen.push(String(url)); super(url, protocols); }
    };
  });
  await page.goto("<the chat URL from task 02>");
  await page.waitForTimeout(3000);
  console.log({ ws: await page.evaluate(() => window.__wsUrls) });
  ```
  `/ws/events` is expected. A `/ws/chat/...` is not.

## Report

- Did the archive clear cleanly, with any toast at all.
- Did the archived chat render its transcript and footer on a direct load.
- The observed socket list, verbatim.

Do **not** continue to "Continue in new chat" — that starts a fresh chat, and
task 04 needs the sidebar uncluttered.
