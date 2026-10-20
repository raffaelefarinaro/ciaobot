# 02 — A chat, start to finish

**Goal:** the core loop. Start a chat, send real turns, watch them stream, stop
one. This is the path a user spends their day in.

**Preconditions:** task 01 finished; the session is unlocked, in the tab you
already have open.

Keep using the same tab throughout. Screenshots go to
`$TMPDIR/ciao-release-walk/<version>/` as `02-chat-<step>.png`.

Use a chat title that is obviously a release smoke test, e.g.
`Release smoke vX.Y.Z`. It makes the leftover obvious.

## Step 1 — start a chat

Open the app root in the tab. Find the composer and the new-chat control
(`aria-label="Start a new chat"` on `HomeIntake`/`PaneHeader`) by role and
visible name rather than coordinates. Click into the composer, type
`Reply with exactly: ok`, and press Enter. Wait until the URL becomes
`/chat/<id>`.

**Watch for:** the project picker. If the composer already names a project, the
chat must start directly — asking *which* project again is the bug #724-era
behaviour came from, and skipping it is the current intent. A picker appearing
here is a finding.

## Step 2 — the turn streams

Wait (up to ~2 minutes) until the reply `ok` renders in the transcript.

**Watch for, in order of how badly it matters:**

- The turn actually completes and the reply renders. An empty bubble, a spinner
  that never resolves, or a turn that ends in an error card is a **blocking
  finding** — do not continue past it; report and stop.
- **No toast fires for a normal turn.** A toast on a plain reply means a
  background or memory pass is surfacing as if the user asked for it.
- The transcript keeps its scroll position at the bottom, and does not jump
  mid-stream.

## Step 3 — a second turn for continuity, then a third turn to stop

Two different things are being proved here, and they need two different turns.

### 3a — continuity

Type `Now reply with exactly: still here` in the composer and press Enter. This
reply is short, so **let it finish** and read it: it answers in the context of
the first turn, which is the point. A turn that starts a fresh session (no
memory of the earlier exchange) is a finding.

### 3b — stopping a turn that is actually running

A turn that has already finished proves nothing about Stop, so this is a
**separate** turn, and it has to be stopped while it is **in flight**.

Ask for a clearly bounded, long, synthetic answer — for example a numbered
list of 40 short invented names, or a long lorem-ipsum paragraph. Keep it a
plain text-generation request: no tools, no private data, no file writes, and
nothing that touches the workspace.

Send it and press **Stop** as soon as the turn is visibly running — the
composer's send button flips to Stop (its accessible name is *Stop
generation*) and the transcript shows the *Working* activity row — do not wait
a fixed number of seconds and do not wait for a short reply, which may be over
before the click lands.

- If the reply is already finished when you get to click, that attempt proves
  nothing: **retry once** with a longer requested output, and stop it sooner.
- If no in-flight stop can be exercised at all, report the stop checkpoint
  **BLOCKED**. Never pass it by inference, and never count a reply that
  finished on its own as a working Stop.

Confirm, after the click:

- the turn ends **promptly** rather than running to completion,
- the composer is usable again immediately (the button is back to *Send
  message*, the textarea accepts text),
- no leftover spinner: the *Working* activity row is gone, no activity spinner
  keeps turning, and the stopped turn shows its collapsed Activity row.

**There is no partial final bubble to keep.** A turn renders one bubble — the
final reply — and everything else the model said on the way there (commentary,
intermediate text) belongs in the **Activity** trace. Stopping a turn
therefore leaves no partial reply in the transcript, and that is correct
behaviour, not a lost answer: the replies that had already completed stay as
bubbles, and the stopped turn's activity stays in the collapsed Activity row
beside them. Do not report a missing partial bubble as a finding.

**Report** each of the three separately. A stop button that leaves the composer
dead is a different bug from one that ignores the click, and the report should
not collapse them.

## Step 4 — file access and network (Workspace only)

Regression check for #1234. In Settings → Workspaces, confirm this chat's
workspace shows the *Workspace only* File access tag. Start a Claude chat in
that workspace and ask the agent, in one message, to run:

- `ciao vault search test --limit 1`
- `curl -sI https://example.com | head -1`

Both must succeed: the vault search returns (even with no hits) and the curl
prints an HTTP status line. A failure in either is a finding.

Then ask the agent to read a file outside this workspace's folder (for example
a file in another workspace's folder). The read must be **refused**. A read that
returns the contents is a finding.

## Checkpoints

- `02-chat-01-composer.png` — composer before sending. Look for: project picker
  absent when a project is named, composer not clipped.
- `02-chat-02-reply.png` — after the first reply. Look for: reply bubble
  rendered and not empty, transcript at the bottom, no stray toast, no error
  card.
- `02-chat-03-streaming.png` — the stop turn mid-stream. Look for: the *Stop
  generation* control visible, the *Working* activity row present, text
  streaming without layout jump, nothing overlapping the composer.
- `02-chat-04-stopped.png` — after stop. Look for: the turn ended (no
  *Working* row, no leftover spinner), the composer enabled and back to
  *Send message*, the earlier completed reply still on screen. There is no
  partial final bubble to look for.

## Verdict

Fill one: **pass** / **finding** / **blocked**.

Record:

- Did the turn complete, in how long?
- Any toast, scroll jump, stuck spinner or error card.
- Continuity: the second turn answered in the context of the first.
- Stop behaviour, stated per the three points above (ends promptly; composer
  usable immediately; no leftover spinner), and whether the stop was caught on
  a turn that was still in flight. State **BLOCKED** if no in-flight stop could
  be exercised.
- The chat URL, so the operator can open it.

Leave this chat open. Task 03 archives it.
