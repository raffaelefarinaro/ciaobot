# Home screen: workspace lanes + shared chat signals

Status: planned, not started
Design date: 2026-08-11
Target repository: `raffaelefarinaro/ciaobot`
Visual reference: <https://claude.ai/code/artifact/8acebb3b-8e89-4b61-83a1-0c03e00c95ac>

Scope is the PWA only (`web/`). No backend, no `ciao/`, no `desktop/` changes.

---

## Goal

The home screen ("jump back in") currently renders every active chat as an
identical card in one recency-sorted grid, below a mascot that occupies the top
half of the window. Three problems follow from that:

1. **Priority is invisible.** A chat blocked on the user's answer, a chat with a
   background agent still running, and a chat last touched three weeks ago are
   the same card at the same weight. The only differentiator is an 8px dot.
2. **Colour means two things at once.** Emerald is both "Personal workspace" and
   "unread" and "streaming"; pink is both "Work workspace" and "needs input" and
   "new chat button". Adding a workspace mutates the meaning of the state
   indicators.
3. **Workspaces interleave.** Personal and Work alternate down the grid, so
   "what is happening at work" can only be answered by reading eight small pills.

This plan replaces the grid with **one lane per workspace, three priority tiers
inside each lane**, and extracts the state indicators into **one shared component
used by both the home screen and the sidebar**.

### Two rules that govern every decision below

- **Fill is state. Hue is ownership.** A solid fill only ever means "needs you".
  Because that one visual property is reserved, hue is free to mean workspace
  everywhere, at any density, at any workspace count.
- **Density is the priority channel.** Urgency is expressed as how much room a
  chat gets — full card, dimmed card, single line, collapsed — not as decoration.
  This survives 60 chats, the light theme, and colour blindness.

---

## Non-goals — do not do these

- **Do not restructure the sidebar.** Its job (workspace-scoped navigator:
  projects → chats → delegates, drag-to-move, rename, archive) is correct and
  distinct from home's job (cross-workspace triage). Only its *signal rendering*
  changes.
- **Do not change what `1`–`9` do.** `ChatLayout.vue` already binds unmodified
  `1`–`9` to `store.switchWorkspace`, deliberately including the automations
  view. Do not fork that per-view. Home reflects `activeWorkspace`; it does not
  redefine the key.
- **Do not remove the dead `recent-section` block** in `ProjectSidebar.vue`
  (currently behind `v-if="false"`). Out of scope; leave it or remove it in a
  separate commit.
- **Do not add a separate "glance strip" above the lanes.** It was designed and
  then cut: the lane header, the sidebar workspace pills, and a strip would all
  show number + name + counts simultaneously. The lane header carries it.
- **Do not introduce new colours.** Everything comes from the existing tokens in
  `web/src/App.vue` and `web/src/lib/workspaceColors.ts`.
- **Do not add a dependency.** No date library; format relative time by hand.

---

## Corrected signal specification

> **Read this section carefully — it corrects the visual reference.**
> The published artifact shows a `4` count badge on a chat card. That is wrong,
> and the mistake was found by reading the store rather than the screenshots.
> `chatUnread()` in `web/src/stores/projects.ts` returns **0 or 1**, not a real
> message count — its own comment says so ("the bell dropdown surfaces the
> list, so an exact per-chat count isn't needed"). A digit badge on a chat would
> therefore always read `1`. The `4` visible on UPWORDO in the current UI is
> `projectUnread()`, which sums the binary per-chat values and *is* a real count.
>
> So unread needs two different forms depending on level, and chat-level unread
> cannot be a solid dot because solid fill is reserved for "needs you".
> **Chat-level unread is carried by title weight; only rollups get a digit.**
> Implement the table below, not the artifact, wherever they disagree.

| State | Level | Form | Source |
|---|---|---|---|
| needs you | chat | Card: filled chip, label `needs you`. Row: 8px filled dot. Hue = workspace. | `chatNeedsInput(id)` |
| working | chat | 10px ring, hue border, pulsing 3px centre dot. Card adds the label `working`. | `isChatStreaming(id)` |
| background agents | chat | Same ring; card label reads `N agents`. | `chatHasBackgroundAgents(id)` |
| unread | chat | **No mark.** Title renders `--fg` at weight 600; read titles use `--fg2` at weight 400. | `chatUnread(id) > 0` (binary) |
| unread | project / workspace | Digit badge, hue background, dark text. | `projectUnread(id)`, `workspaceUnread(ws)` |
| needs you | project / workspace | 8px filled dot next to the name. | `projectNeedsInput(id)`, **new** `workspaceNeedsInput(ws)` |
| looping | chat | `↻` (`&#10227;`) glyph. Hue when running, `--fg3` when stopped. | `taskStore.loops` by `web_chat_id` |
| retry pending | chat | `↻` glyph in `--warning`, distinct title. Sidebar only (home has no retry indicator today; keep it that way). | `chat.retry?.status === 'pending'` |
| remote | chat | Existing `remote` chip and disabled state. Unchanged. | `chat.local === false` |
| quiet | chat | Nothing. Relative timestamp only. | `last_activity_at` |

Precedence when several apply, highest first:
`needs you` → `working` → `background agents` → `retry pending` → `looping` (a
modifier, may co-render with the above) → `unread` (title weight, always
co-renders) → `quiet`.

`prefers-reduced-motion: reduce` must disable the pulse and leave the dot at
~0.65 opacity.

---

## Step 1 — Shared `ChatSignals` component, both surfaces, no layout change

Ships alone. Fixes the two-vocabulary problem and must land before any layout
work, because doing home first is precisely what makes the same chat render two
different ways on one screen.

### 1a. New file `web/src/components/ChatSignals.vue`

```
props:
  chatId:  string       (required)
  density: 'card' | 'row'   (default 'row')
  hue:     WorkspaceColorId (optional; falls back to inherited --accent)
```

- Implements the state table and the precedence order above.
- Reads `useProjectStore()` and `useTaskStore()` directly — do not thread state
  down as props; both current call sites already access the stores.
- `density: 'card'` renders labelled chips; `density: 'row'` renders bare marks.
- **Does not render the unread title weight.** That is a property of the title
  element, which lives in the parent. Expose a helper the parents can use:
  `store.chatUnread(id) > 0`. Document this in a comment in the component so the
  next reader does not go looking for unread inside it.
- Every mark keeps a `title` and an `aria-label`. The current `?` badge, the
  amber-vs-accent dot pair, and the bare `unread-dot` all disappear.

### 1b. Move the loop lookup into the store

`loopsByChat` is currently duplicated: `HomeRecentChats.vue` (lines ~117-138)
and `ProjectSidebar.vue` (`chatLoopBadge`). Add one computed to the task store
(`web/src/stores/tasks.ts`), keyed by `web_chat_id`, returning
`{ count, running }`, and have `ChatSignals` consume it. Delete both copies.

### 1c. Relative time helper — extract, do not create

**A `formatRelative()` already exists** at `ProjectView.vue:504`, private to that
component, with a different vocabulary: `just now`, `5m ago`, `3h ago`,
`3d ago`, then an absolute `Aug 3` past one week. Do not write a second one — the
app would grow two relative-time dialects that drift apart.

Move it to `web/src/lib/relativeTime.ts` and widen it:

```
formatRelative(iso: string, opts?: { suffix?: boolean; now?: Date }): string
```

- `suffix: false` (default for dense chat rows) → `now`, `12m`, `3h`, `2d`, `3w`, `4mo`
- `suffix: true` (keeps `ProjectView`'s file listings reading as they do today)
  → `just now`, `12m ago`, …
- injectable `now` so it is testable without fake timers
- migrate all three `ProjectView` call sites (lines ~146, ~165, ~179) in the same
  change, and keep its absolute-date fallback past a week for file listings

`last_activity_at` already exists on `ChatInfo` and is currently unused on home.

### 1d. Adopt in all four call sites

The chain is duplicated in **four** places, not two. All four must move together;
adopting a subset is what leaves one chat rendering two ways on one screen.

- `web/src/components/HomeRecentChats.vue` — replace the mark chain
  (lines ~28-38) with `<ChatSignals :chat-id density="card" />`; add the
  relative timestamp; apply unread title weight.
- `web/src/components/ProjectSidebar.vue` — replace the mark chain
  (lines ~479-491) with `<ChatSignals :chat-id density="row" />`; apply unread
  title weight to `.chat-title`. Keep the `remote` chip and the `···` actions
  button where they are.
- `web/src/components/ProjectView.vue` — the chat rows (lines ~89-92) render the
  same `spinner-dot` / `needs-input-badge` / `badge` chain. Same `row` density.
  Note this file also has the binary-unread bug: line ~92 prints
  `{{ store.chatUnread(...) }}` as a digit that can only ever read `1`.
- `web/src/components/ChatPanel.vue` — the subchat banner (lines ~191-194)
  renders the chain **as prose** (`· working`, `· agents running`,
  `· needs input`, `· unread`). Replace with the `row` density component.
- Project headers in the sidebar (lines ~396-398): replace the dot + badge pair
  with the filled-dot-plus-digit form from the table.

Delete the now-unused CSS: `.needs-input-badge`, `.unread-dot`,
`.spinner-dot.bg-agents`, and the duplicated `.loop-mark` rules. Grep before
deleting — `spinner-dot` is also used by the schedules list in the same file and
by `SchedulePanel`; **do not remove that one.**

### 1e. Tests

New `web/src/components/__tests__/ChatSignals.test.ts`:

- one case per row of the state table, asserting the rendered mark
- precedence: a chat that both needs input and is streaming renders only the
  needs-input form
- `density: 'row'` renders no text labels
- reduced-motion: assert the class/attribute hook exists (jsdom will not
  evaluate the media query, so assert the rule is present in the component, or
  extract the decision into a testable helper)

New `web/src/lib/__tests__/relativeTime.test.ts` — boundaries at 59s, 60s,
59m, 60m, 23h, 24h, 6d, 7d, 4w.

---

## Step 2 — Tier the list

Still one column; no lanes yet. Most of the legibility gain lands here.

### 2a. Grouping

In `HomeRecentChats.vue`, derive from `store.activeChatsAll`:

```
tier1  needsYou  chatNeedsInput
tier2  working   isChatStreaming || chatHasBackgroundAgents
tier3  quiet     everything else, activity newer than 7 days
tier4  older     activity older than 7 days  → collapsed
```

Within a tier keep the existing recency sort. A chat appears in exactly one
tier. Render a tier label (uppercase, `--fg3`, hairline rule) above each
non-empty tier, and for tier 1 only, render the label plus
`// nothing needs you here` when the tier is empty — an absence that reports
itself rather than vanishing.

### 2b. Forms

- Tier 1: card, `background: #23213f`-equivalent from tokens, 2-line title
  clamp, the pending question line, meta row.
- Tier 2: same card, dimmed (`--fg2` title at weight 400).
- Tier 3: single-line row, no card chrome, 2px left border in the workspace hue
  at ~45% alpha, right-aligned timestamp. Age opacity ramp: `<24h` = 1,
  `<7d` = 0.72, older = 0.55.
- Tier 4: one disclosure control, `N more, older than a week`, expanding into
  tier-3 rows. Collapsed by default. `activeChatsAll` is uncapped today, so this
  is what bounds the screen permanently.

### 2c. The pending-question line

Tier 1 cards show the first line of the outstanding question. `pending_question`
exists on `ChatInfo` and `parseQuestions()` already parses it in the store.
Expose a small store getter returning the first question's text, clamp to two
lines in CSS. This is the highest-leverage element on the screen — it often lets
the user answer without opening the chat. If parsing yields nothing, omit the
line entirely rather than rendering an empty element.

### 2d. Two-line titles

Replace the single-line ellipsis with a 2-line clamp
(`-webkit-line-clamp: 2`) on tier-1 and tier-2 cards. Tier 3 stays one line.
Truncating "Tracking issues assigned to user…" exactly where the useful part
starts is one of the reported problems.

---

## Step 3 — Lanes

### 3a. Grouping and order

Group `store.activeChatsAll` by `projectFor(chat.chat_id)?.workspace`. Order the
lanes to match `store.workspaceOptions` exactly, so lane *n*'s number badge
equals the sidebar pill's `workspaceShortcut` for the same workspace. Chats
whose project cannot be resolved go in a trailing unlabelled lane rather than
being dropped.

Render every workspace in `workspaceOptions`, including ones with no chats — an
empty lane that says so is more legible than a workspace silently absent.

### 3b. Lane header

`[n] WORKSPACE · <summary> ............ + new`

- `[n]` — the number key badge, hue border and text. This is the discoverability
  fix: the shortcut exists today but is only visible in the sidebar.
- summary — `<b>2 need you</b> · 5 quiet`, built from the tier counts. Bold part
  in the workspace hue. Omit zero-value clauses; render `all quiet` when tiers 1
  and 2 are both empty.
- `+ new` — ghost button, dashed hue border, creating a chat in *that*
  workspace's General project. This replaces the two saturated bottom buttons in
  `ChatLayout.vue`'s `empty-actions`; reuse `createWorkspaceChat`.
- 2px solid hue bottom border on the header — the lane's identity anchor.

### 3c. Drop the per-card workspace chip

`.home-recent-ws` goes away: the lane header names the workspace. That reclaims
a row per card, which is what pays for the two-line title and the question line.
Keep the 2px hue left border on cards so a card remains attributable when
screenshotted or scrolled away from its header.

### 3d. Container

Widen the home container from `max-width: 560px` to ~1040px, two lanes on a
`1fr 1fr` grid with a 20px gap, `align-items: start`. Sync the `empty-state`
wrapper in `ChatLayout.vue` if it constrains width.

### 3e. Shrink the mascot to a header mark

In both copies of the `empty-state` block in `ChatLayout.vue` (the split-view
copy and the `v-else` copy — **both**, they are easy to miss and the arrow-key
ref bug in the file history came from tagging only one):

- 30px mark, left-aligned, beside a one-line status: `2 chats need you. One
  agent still working.` Keep the click-for-greeting easter egg and the
  hover/leave handlers.
- Keep the large centred mascot for the genuine empty state — `activeChatsAll`
  empty. That is the only case where a first-run screen has nothing better to
  show. This case is **not** in the visual reference; implement it from this
  paragraph.

### 3f. New store selector

`web/src/stores/projects.ts`:

```
function workspaceNeedsInput(ws: WorkspaceName): number
```

Mirror `workspaceUnread`'s shape: filter `projects` by workspace, sum
`projectNeedsInput`. Export it in the store's return object (~line 3977, beside
`projectNeedsInput`).

Use it for **both** the lane summary and the sidebar workspace pills. The pills
currently fall back `workspaceIsStreaming` → `workspaceUnread`, so they cannot
show the most urgent state at all; put `workspaceNeedsInput` first in that
chain.

### 3g. Sidebar pills

Apply the same grammar as the lane header, tighter: `[n] Name` plus the
filled-dot / ring / digit marks from the state table. These pills are what shows
cross-workspace state *while the user is inside a chat* — exactly when home is
not on screen — so keep them and home consistent.

---

## Step 4 — Keyboard and the breakpoint

### 4a. Rewrite `onArrow`

The current implementation infers a column count from
`getComputedStyle(grid).gridTemplateColumns` and walks a flat DOM-ordered list.
That does not survive lanes, and today it lets Left/Right slide between
workspaces mid-row with no visual logic. New mapping:

| Key | Behaviour |
|---|---|
| `↑` / `↓` | Move within the focused lane, as one sequence across tier boundaries |
| `←` / `→` | Move to the adjacent lane, landing at the nearest index (clamped) |
| `Enter` | Unchanged — cards stay `<button>`, browser activates natively |
| `1`–`9` | Unchanged — `switchWorkspace` |
| `Esc` | Unchanged |

Implementation: build a 2-D model — an array of lanes, each an array of
focusable elements in visual order — rather than reading computed CSS. Track
`(lane, index)`. Keep the existing contract: `onArrow` returns `true` when the
key was consumed, including at edges, so the page never scrolls; `false` when
there is nothing to navigate.

**Focusability**: tier-3 rows and the tier-4 disclosure must become real
`<button>`s and join the sequence, otherwise arrows skip most of a lane once
tier 3 stops being cards. The disclosure is the last stop in its lane. Remote
chats stay disabled and skipped.

### 4b. Breakpoint

Under ~820px: one expanded lane (the `activeWorkspace` one) plus a peek row per
other workspace — `[n] Name · 1 working · 3 quiet`. `←`/`→` become inert; peek
rows join the `↑`/`↓` sequence and `Enter` on one expands that lane. Verify the
lane grid collapses to a single column and nothing scrolls horizontally.

### 4c. Home reflects `activeWorkspace`

Pressing `2` still calls `switchWorkspace`. On home, the corresponding lane gains
emphasis and `scrollIntoView({ block: 'nearest' })`. Do not add a second
meaning to the key.

---

## Tests

### Update

`web/src/components/__tests__/HomeRecentChats.test.ts` will break by design —
it asserts `.home-recent-card` counts and flat Left/Right motion. Rewrite:

- seed chats across two workspaces and assert lane grouping and lane order
  matches `workspaceOptions`
- tier assignment per the step-2 table, including a chat that qualifies for two
  tiers landing only in the highest
- `↑`/`↓` stays within a lane; `←`/`→` crosses lanes; edges still return `true`
- no chats → `onArrow` returns `false`
- tier-3 rows and the disclosure are focusable and in the sequence
- the existing `seedChats` helper needs a `workspace` argument and a second
  project

`web/src/components/__tests__/ChatLayout.test.ts` stubs `HomeRecentChats` in
most cases, but one case (~line 738) deliberately does not — that one needs
updating for the new DOM.

### Add

- `ChatSignals.test.ts` and `relativeTime.test.ts` (step 1e)
- store test for `workspaceNeedsInput` in `web/src/stores/projects.test.ts`,
  including the zero case and a chat whose project is in another workspace
- a tier-grouping unit test that does not go through the DOM, if the grouping is
  extracted into a helper (preferred — it makes the component test smaller)

---

## Verification

Run from `web/`:

```
npm run test            # vitest run
npm run build           # vue-tsc --noEmit && vite build — type errors gate this
npm run lint
```

`pytest tests/` is not required: no Python changes. `./scripts/check-desktop.sh`
is not required: nothing under `desktop/` changes.

Manual checks, per `CLAUDE.md`:

- keyboard focus is visible on every focusable element, including tier-3 rows
- browser zoom to 200% — lanes must reflow, not clip
- mobile touch targets stay ≥44px (`--touch`); tier-3 rows are the risk
- light theme (`:root.theme-light`) — verify the filled chip's dark text stays
  legible on every workspace hue, and that the age-opacity ramp still reads
- both mascot copies in `ChatLayout.vue` behave identically
- a workspace with zero chats renders an empty lane
- `1`/`2` switch workspace from home, from an open chat, and from automations

---

## Acceptance checklist

- [ ] `ChatSignals.vue` is the only place chat state marks are rendered; no mark
      markup remains in `HomeRecentChats.vue`, `ProjectSidebar.vue`,
      `ProjectView.vue`, or `ChatPanel.vue` (the last renders it as prose today)
- [ ] Exactly one `formatRelative` exists, in `web/src/lib/`, with
      `ProjectView.vue`'s three call sites migrated to it
- [ ] No `?` badge, no amber-vs-accent dot pair anywhere
- [ ] Chat-level unread is title weight; digits appear only on project and
      workspace rollups
- [ ] A solid fill appears if and only if something needs the user
- [ ] Hue is workspace-derived at every call site; no state uses a fixed hue
- [ ] One lane per entry in `workspaceOptions`, in that order, number badge
      matching the sidebar pill
- [ ] Lane header carries the count summary; there is no separate glance strip
- [ ] Tiers render in order with labels; tier 1 reports its own emptiness
- [ ] Chats older than 7 days are collapsed behind one disclosure
- [ ] Relative timestamps everywhere; age ramps opacity
- [ ] `↑↓` within lane, `←→` across lanes, edges consume the key
- [ ] Tier-3 rows and the disclosure are focusable buttons
- [ ] `1`–`9` still means exactly `switchWorkspace`, in all views
- [ ] Large mascot only when `activeChatsAll` is empty
- [ ] `npm run test`, `npm run build`, `npm run lint` all pass
- [ ] Verified in light theme and at 200% zoom

---

## Commit sequence

One commit per step, each independently revertable:

1. `feat(pwa): shared ChatSignals component for home and sidebar`
2. `feat(pwa): tier the home chat list by priority`
3. `feat(pwa): group the home screen into workspace lanes`
4. `feat(pwa): lane-aware arrow navigation and narrow-width lanes`

Steps 1 and 2 carry most of the legibility gain and touch no layout, so they can
ship before 3 and 4 are reviewed. Use plain factual commit messages per
`CLAUDE.md`; run `/code-review --fix` before opening the PR per the `pr` skill.
