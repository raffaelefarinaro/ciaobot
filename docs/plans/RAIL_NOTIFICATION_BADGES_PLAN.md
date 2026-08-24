# Rail Notification Badges — consolidate attention signals onto destination icons

## Resume block

- Status: complete (automated verification passed)
- Current checkpoint: C7 (implementation and automated verification complete)
- Next action: optional manual visual pass at desktop/mobile widths
- Blocker: none
- Implementation repository: /Users/raffaelefarinaro/repos/ciaobot
- Generated plan output: docs/plans/RAIL_NOTIFICATION_BADGES_PLAN.md
- Visual companions: docs/plans/RAIL_NOTIFICATION_BADGES_PLAN.html
- Verified on: 2026-08-24 against web/src/components/ProjectSidebar.vue, web/src/components/NotificationBell.vue, web/src/components/PaneHeader.vue, web/src/components/HomeRecentChats.vue, web/src/components/HousekeepingStrip.vue, web/src/stores/projects.ts, web/src/stores/proposals.ts (+ its tests), web/src/stores/housekeeping.ts, web/src/lib/homeLanes.ts, DESIGN.md

This block is the handoff contract. Any model that resumes the work should read it first, then read the current checkpoint and open questions before doing anything else.

## Outcome and user value

Every attention signal in the PWA lives on the icon of the place where the user acts on it:

- The **chats** rail icon shows a global count of chats needing attention (unread or needs-input).
- The **memory** rail icon shows a global count of queued proposals to review.
- Each section's **workspace toggle** shows per-workspace counts relevant to that section (chats already does this; memory/review gains it).
- The bell icon and its dropdown menu are removed; the home screen remains the "inbox" that lists and tiers those chats.

Value: one click fewer to reach any pending item (icon → destination instead of icon → dropdown → row), no duplicated inbox surface, and proposal queues become visible from anywhere in the app instead of only inside the memory view.

## Scope and non-goals

In scope:

- Global count badges on the chats and memory nav-rail icons (`ProjectSidebar.vue`).
- Per-workspace proposal counts on the memory/proposals workspace toggle.
- Removal of `NotificationBell.vue` from both mount points (sidebar rail, `PaneHeader`) and deletion of the component.
- Relocation of the bell's "Mark all read" action.
- Warning dot on the settings rail icon while a blocking housekeeping action exists (D-06).
- Accessibility labels for every new badge; DESIGN.md alignment.

Out of scope for this release:

- Native macOS tray menu and tray badge behavior (`DESIGN.md` line 205 governs it; unchanged).
- Backend/API changes — all counts are client-side derivations from existing stores.
- Home lane redesign (`homeLanes.ts` tiers stay as they are).
- Desktop push-notification behavior, toasts, and `document.title` unread prefix (all keep working unchanged).

## Current-state evidence

Observed (files read on 2026-08-24):

- The nav rail is rendered in `web/src/components/ProjectSidebar.vue:24-112`: chats (`nav-item--working` state), automations (`nav-item--warning`), memory (active for both `memory` and `proposals` modes), settings (update dot via `.nav-item-badge` at line 110), then `<NotificationBell>` at line 112.
- `NotificationBell.vue:120-132`: `totalActionRequired` sums over **all** non-archived `store.chats` where `chatNeedsInput(id) || chatUnread(id) > 0`. Its panel lists those chats sorted by recency with a "Mark all read" button calling `store.markAllRead()`.
- The bell is mounted twice: `ProjectSidebar.vue:112` (rail) and `PaneHeader.vue:32` (header trail, present on every pane including mobile layouts behind the hamburger).
- Chats/project-mode workspace toggle (`ProjectSidebar.vue:697-735`) already renders per-workspace signals: needs-input dot (`workspaceNeedsInput`), streaming ring (`workspaceIsStreaming`), unread count badge (`workspaceUnread`).
- Schedules-mode workspace toggle (`ProjectSidebar.vue:121-145`) already renders a missed-run badge via `missedCountFor`.
- Memory/proposals-mode workspace toggle (`ProjectSidebar.vue:366-381`) renders **no** per-workspace counts — this is the gap.
- The Review segment in the view switcher shows the **global** `proposals.rows.length` (`ProjectSidebar.vue:406`). The proposals store exposes `scopedRows(ws)` / `visibleRows(ws)` / `kindCounts(ws)` (`web/src/stores/proposals.ts:61-85`); `scopedRows` includes workspace-global rows in every scope (`proposals.test.ts:49-56`), so `scopedRows(ws).length` is the correct per-workspace number.
- Store exports available today (`projects.ts:5102`): `chatUnread`, `chatNeedsInput`, `workspaceUnread`, `workspaceNeedsInput`, `totalUnread`, `markAllRead`. There is no exported "chats needing attention" aggregate — the bell computes it inline.
- The home screen already acts as an inbox: needs-you tier first, then working, then unread (`web/src/lib/homeLanes.ts:31-57`).
- Ambient signals that survive bell removal: `document.title` `(n)` prefix from `store.totalUnread` (`ChatLayout.vue:617-619`, `lib/appTitle.ts`), in-app toasts for background completions. Desktop push notifications, toasts, and the tray badge are separate channels and are **not** affected by this plan.
- System-state signals observed: update available renders a dot on the settings rail item (`ProjectSidebar.vue:96-110`); `gws_health` SSE events raise a toast (`web/src/lib/types.ts:405`, handled at `projects.ts:3648`); actionable system items surface as housekeeping tiles from `/api/housekeeping` via `HousekeepingStrip.vue` (with a blocking emphasis class). No test file exists for NotificationBell itself. `PaneHeaderBrand.test.ts` mounts the real PaneHeader, which imports the bell — verify it makes no bell assertions when removing the import.
- Proposal data is currently fetched by `ProposalReviewPanel.vue:607` on review-panel mount; the sidebar's existing `proposals.rows.length` count is therefore not guaranteed to be populated while the user is on home, chats, settings, or automations.
- Housekeeping data is currently initialized by `HousekeepingStrip.vue:59-61`, which is mounted on the home screen. A settings warning dot cannot rely on that component having mounted first.
- Existing test references include a `NotificationBell` mock in `PaneHeaderBrand.test.ts`, stubs in `ProjectSidebar.test.ts` and `ProjectSidebarReview.test.ts`, and a module mock in `mountSmoke.test.ts`; all must be removed or updated with the runtime import.
- Stale-comment risk: `ProjectSidebar.vue:1958` ("Mirrors NotificationBell's unread badge") must be reworded.

Assumed (to verify during implementation):

- A quiet action beside `HomeRecentChats.vue:13` (`jump back in`) is an acceptable home-inbox location for "mark all read"; there is no existing global action slot in that component.

## Recommended direction

1. **Store-level aggregate first.** Add an exported computed to the projects store, e.g. `attentionChatCount` (non-archived chats where `chatNeedsInput || chatUnread > 0`, all workspaces). This moves the bell's inline logic (`NotificationBell.vue:120-125`) into one tested place and serves the chats rail badge and aria label. Keep `totalUnread` as the condition for "mark all read" because `markAllRead()` cannot clear a pending question or permission request.
2. **Rail badges.** Add a count pill to the chats and memory nav items, visually identical for both (same red pill the bell used: 16px, 10px font, fully rounded). Reuse/extend `.nav-item-badge` into a counting variant rather than inventing a style. Aria-labels: "chats — N need attention", "memory — N to review". Existing states stay and compose: chats keeps its working tint, settings keeps its update dot.
3. **Load proposal and housekeeping signals globally.** Make the proposals store fetch safe to call from the always-mounted sidebar (deduplicate an in-flight request or add an `ensureLoaded()` path), then initialize it there so the memory rail badge works from every route. Likewise initialize the housekeeping store from the sidebar; its existing `init()` remains idempotent, so `HousekeepingStrip` can continue calling it on home. Do not add a new API or persisted state.
4. **Memory/proposals workspace toggle counts.** In `ProjectSidebar.vue:366-381`, add the same badge pattern the schedules toggle uses, showing `proposals.scopedRows(workspace.name).length` with title/aria "N to review".
5. **Remove the bell.** Delete `NotificationBell.vue`, both runtime mounts, its imports, and test mocks/stubs; relocate "Mark all read" beside the `jump back in` heading (see D-03). Reword comments that reference the bell.
6. **Docs.** Update `DESIGN.md` (badge grammar note under Components/Badges; the overview sentence about signals) so the system description matches the implementation.

Wire-format/ownership notes: no API or persisted-state changes. The only new shared surface is the projects-store aggregate, which is additive and reversible.

## Alternatives and rejected options

### Keep the bell as the sole aggregator, add only the memory badge

Rejected: the bell duplicates what the home screen already tiers better, costs one extra click per navigation, and keeps two competing "notifications" surfaces. The user explicitly wants destination-scoped signals.

### Keep a bell only in PaneHeader (mobile) while removing it from the rail

Rejected for now: splits the model across breakpoints, and mobile still has the title-prefix, toasts, and sidebar badges after opening. Revisit if mobile review shows missed signals (logged as fog, see below).

### Weight needs-input higher than unread in the chats badge (e.g., amber vs red)

Deferred: the bell never distinguished them numerically either; home lanes carry the distinction. Adding severity tiers to badges is a design-system change beyond this consolidation.

## Visual review

The HTML companion (`docs/plans/RAIL_NOTIFICATION_BADGES_PLAN.html`) answers the layout/state question: what the collapsed rail looks like before vs. after, and what each section's workspace toggle shows. It is a static chrome mockup, grounded in the real nav labels and badge styles; interaction is limited to toggling the before/after state. Markdown remains canonical for decisions.

## Decisions and hard-to-reverse bets

| ID | Decision | Rationale | Status |
| --- | --- | --- | --- |
| D-01 | Rail badges are **global** across workspaces | User decision; the rail is not workspace-specific. Workspace scoping happens in each section's toggle | Accepted |
| D-02 | Remove `NotificationBell.vue` entirely (both mounts) | Home screen is the inbox; destination icons carry the signals | Accepted |
| D-03 | "Mark all read" moves beside the home `jump back in` heading, shown only when `totalUnread > 0` | Preserves the function without the bell and avoids a no-op button when only needs-input items remain | Accepted |
| D-04 | Memory/proposals workspace toggle badges use `scopedRows(ws).length` | Includes workspace-global proposals in every scope, matching queue behavior (`proposals.test.ts:49-56`) | Accepted |
| D-05 | Client-side only; no backend changes | All inputs already exist in stores | Accepted |
| D-06 | Warning dot on the settings rail icon while any **blocking** housekeeping action exists; counts stay off system state | System items are state, not a queue — they reuse the existing automations-style warning grammar instead of the count pill; makes "system needs you" visible from anywhere without a new surface. If update and housekeeping warnings coexist, render one dot and combine the accessible/title text | Accepted |

## Open questions with recommended defaults

| ID | Question | Recommended default | Status |
| --- | --- | --- | --- |
| Q-01 | Should the Review segment count (`ProjectSidebar.vue:406`) switch from global to active-workspace scoped once tiles are scoped? | Leave global this release; revisit after living with scoped tiles | Open |
| Q-02 | Badge display cap (e.g., "99+")? | No cap — show the raw total, matching the old bell and the tray rule in DESIGN.md | Open |
| Q-03 | Does the settings section need any workspace-tile signal? | No — settings has no workspace toggle today and its signal (update available) is global on the rail icon | Open |

## Not yet specified (fog of war)

- Whether the native macOS tray should later mirror these same per-surface counts (it currently mirrors unread chats per DESIGN.md). Out of scope now; may graduate into an open question when the PWA model settles.
- Whether the automations rail item should ever gain a global missed-runs badge (today it uses a warning tint only). Not phrasable sharply until there is user friction evidence.

## Feedback and decision log

| ID | Location | Feedback | Decision | Status | Evidence |
| --- | --- | --- | --- | --- | --- |
| F-01 | Rail / memory icon | Items to review should badge the memory icon like notifications do | Accepted — memory rail badge, global count | accepted | chat 2026-08-24; D-01, direction step 2 |
| F-02 | Bell icon/menu | If chat notifications move to icons, the bell icon and menu can go | Accepted direction with caveats raised (mobile, mark-all-read, semantics) | accepted | chat 2026-08-24; D-02, D-03 |
| F-03 | Counts semantics | Rail counts global; workspace tiles show section-relevant per-workspace counts like the chats page already does | Accepted — D-01, D-04; schedules/chats tiles unchanged, memory tiles gain counts | accepted | chat 2026-08-24 |
| F-04 | Notification channels | User asked whether received (push/toast) notifications stay | Confirmed yes — only the bell icon+panel is removed; desktop push, toasts, title prefix, tray badge untouched | answered | chat 2026-08-24; scope non-goals + evidence |
| F-05 | Settings page | User asked whether system notices (gws login, update available) should show in settings too | Update dot already on settings rail icon; gws health = toast + housekeeping tiles; added blocking-housekeeping warning dot on settings icon as D-06/T-07 instead of counts on system state | accepted | chat 2026-08-24; D-06, T-07 |

## Implementation checkpoints

Each checkpoint has an exit condition so another model can tell whether the work is actually ready to move on.

### C0. Start or resume

- Read the Resume block; read this plan and the live files named above.
- Confirm the repository still matches the current-state evidence (bell mounted in exactly two places, memory toggle uncounted).

Exit evidence: discrepancies recorded here before any edit.

### C1–C4. Grounding, direction, artifacts, review

Done in this document and the HTML companion. Exit evidence: sections above.

### C5. Get approval

- Surface the plan; capture feedback in the log above.
- Resolve Q-01..Q-03 or leave open with defaults.

Exit evidence: status set to `approved` by the user on 2026-08-24; D-01, D-05, F-01 through F-05 are accepted or answered. Q-01 through Q-03 retain their documented defaults.

### C6. Implement

Ordered tasks:

- T-01 Projects store: add `attentionChatCount` computed + export; unit tests in `projects.test.ts` covering needs-input, unread, archived exclusion, nested-delegate behavior, and cross-workspace totals (mirrors `NotificationBell.vue:120-125` logic).
- T-02 Signal loading: make proposal fetch safe to initialize from the sidebar without racing the review panel; initialize proposals and housekeeping from the always-mounted sidebar so global badges work before visiting memory or home. Add focused store/component tests for the initial-load path.
- T-03 `ProjectSidebar.vue` rail: add count pill to chats and memory links (shared counting-badge class derived from `.nav-item-badge`/`.bell-badge` metrics); aria-labels; keep existing working/warning/update states composing.
- T-04 `ProjectSidebar.vue` memory/proposals toggle: per-workspace `scopedRows(ws).length` badge with title/aria.
- T-05 Remove `NotificationBell.vue`; remove mounts in `ProjectSidebar.vue:112` and `PaneHeader.vue:32`; remove test mocks/stubs in `PaneHeaderBrand.test.ts`, `ProjectSidebar.test.ts`, `ProjectSidebarReview.test.ts`, and `mountSmoke.test.ts`; add "mark all read" beside `jump back in`, gated by `store.totalUnread > 0`; reword stale comments (`ProjectSidebar.vue:1958` and any other bell references).
- T-06 Tests: add sidebar-level badge tests, memory workspace-count tests, settings warning-dot tests, global-load tests, and mark-all-read placement/visibility tests.
- T-07 Settings rail warning dot (D-06): use the housekeeping store's `actions.some(action => action.blocking)` after global initialization; merge it with the existing update dot and `nav-item--warning` styling without rendering duplicate dots.
- T-08 Docs: `DESIGN.md` badge grammar + overview wording; `web/README.md` if it mentions the bell.

Exit evidence: T-01 through T-08 are implemented; no runtime or test `NotificationBell` references remain in `web/src`.

### C7. Verify

- `cd web && npm test` — passed: 70 test files, 769 tests.
- `cd web && npm run lint` — passed with 11 existing warnings (`any` and unused symbols in unrelated/pre-existing areas); no errors.
- `cd web && npm run build` — passed; Vite emitted only the existing chunk-size and dynamic-import warnings.
- Manual pass in dev: not run in this session. Still needed to confirm badge appearance/disappearance, proposal refresh after accept/dismiss, mark-all-read placement, and mobile width behavior.

Exit evidence: automated commands green; manual visual verification remains explicitly pending. No backend suite needed (`pytest tests/` unaffected — no Python changes).

### C8. Close or hand off

- Set final status `complete`; record verified commit.

## Verification and rollout

Single-PWA change, no data migration, instantly reversible by reverting the commit. Rollout risk is limited to visual regressions in the rail and header; mitigated by the component tests in T-05 and the manual checklist in C7. Verified behavior will be distinguished from assumed deployment: only what ran under `npm test`/`npm run build` and the manual dev-server pass counts as verified.
