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

## Step 3 — a second turn, then stop it

Context continuity is the thing worth proving: the second turn should behave
like a conversation, not a fresh session.

Type `Now reply with exactly: still here` in the composer and press Enter. Wait
about three seconds so the reply is mid-stream, take the streaming screenshot,
then find the stop control and press it. Confirm:

- the turn ends promptly rather than running to completion,
- the partial reply stays in the transcript (it is not discarded),
- the composer is usable again immediately.

**Report** each of the three separately. A stop button that leaves the composer
dead is a different bug from one that ignores the click, and the report should
not collapse them.

## Checkpoints

- `02-chat-01-composer.png` — composer before sending. Look for: project picker
  absent when a project is named, composer not clipped.
- `02-chat-02-reply.png` — after the first reply. Look for: reply bubble
  rendered and not empty, transcript at the bottom, no stray toast, no error
  card.
- `02-chat-03-streaming.png` — second turn mid-stream. Look for: stop control
  visible, text streaming without layout jump, nothing overlapping the
  composer.
- `02-chat-04-stopped.png` — after stop. Look for: partial reply kept, composer
  enabled, no leftover spinner.

## Verdict

Fill one: **pass** / **finding** / **blocked**.

Record:

- Did the turn complete, in how long?
- Any toast, scroll jump, stuck spinner or error card.
- Stop behaviour, stated per the three points above (ends promptly; partial
  reply kept; composer usable).
- The chat URL, so the operator can open it.

Leave this chat open. Task 03 archives it.
