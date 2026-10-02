# 03 — Archive, and the archived chat

**Goal:** archiving is the quiet end of the loop, and it quietly starts two
things — a memory pass and an index write. Both are easy to break and invisible
until they are.

**Preconditions:** task 02 left a chat open at `/chat/<id>`. Keep using the same
tab. Screenshots go to `$TMPDIR/ciao-release-walk/<version>/` as
`03-archive-<step>.png`.

## Step 1 — archive it

Note the current URL. The archive control is the header's one labelled action
(`ChatPanel.vue`: `aria-label` + a visible `Archive` word — it gets words, not
just a glyph, so it is findable by role and name). Click it, wait for the
confirm dialog (`ConfirmDialog`, confirm label `Archive`), take the dialog
screenshot, confirm, and give it about two seconds. Note the URL afterwards.

**Watch for:**

- The pane clears optimistically and lands somewhere sensible (Home, or the
  next chat) — not a blank route.
- **No toast.** Archiving spawns a memory pass; that pass runs as an ordinary
  chat, and its `chat_result_ready` event used to raise a "Memory pass · …"
  toast. The event itself still publishes — only the toast is guarded. A toast
  here is a known regression shape and worth reporting precisely.
- No error card.

## Step 2 — reopen the archived chat

This is the #619 failure, and it is why the route exists. Navigate **directly**,
by URL, so the boot URL restore, the route watcher and the store all have to
agree. Before loading, arrange for a WebSocket recorder to run before the app's
first script (an init script that wraps `window.WebSocket` and pushes each
constructed URL onto `window.__wsUrls`). Then load the chat URL from task 02,
wait about 3 seconds, and read `window.__wsUrls` by evaluating JavaScript in the
view.

**Must be true:** the transcript renders — not an empty pane. #619 produced
`/chat/<archived-id>` resolving to *no panel at all*, so the archived footer was
unreachable. An empty pane with a working URL is exactly that bug.

**Watch for:**

- The archived footer is present: a link to the memory pass, and "Continue in
  new chat".
- **The page is inert** — no socket dial, no spinner, no "reconnecting". A
  read-only transcript that opens a live chat socket is a finding.
  `/ws/events` is expected. A `/ws/chat/...` is not.

## Checkpoints

- `03-archive-01-dialog.png` — the confirm dialog. Look for: dialog centred,
  label reads `Archive`, nothing clipped.
- `03-archive-02-after.png` — just after archiving. Look for: sensible landing
  route, not a blank pane, no toast, no error card.
- `03-archive-03-archived-chat.png` — direct load of the archived chat. Look
  for: transcript rendered, archived footer with memory-pass link and "Continue
  in new chat", no spinner or "reconnecting".

## Verdict

Fill one: **pass** / **finding** / **blocked**.

Record:

- Did the archive clear cleanly, with any toast at all.
- Did the archived chat render its transcript and footer on a direct load.
- The observed socket list, verbatim.

Do **not** continue to "Continue in new chat" — that starts a fresh chat, and
task 04 needs the sidebar uncluttered.
