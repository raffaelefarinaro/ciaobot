# Home notice windows before v1.1.0

## Resume block

- Status: implemented on `feature/home-notice-windows`; release PR [#907](https://github.com/raffaelefarinaro/ciaobot/pull/907) was closed unmerged so a new candidate can include this work.
- Current checkpoint: C7 — review and merge the UI branch into `develop` before recutting v1.1.0.
- Next action: review the UI PR, then cut a fresh release candidate from updated `develop`.
- Blocker: none; the user approved separate in-flow windows, session-local X, reopening, and retained durable Hide buttons.
- Implementation repository: `/Users/raffaelefarinaro/repos/ciaobot`.
- Plan: `docs/plans/home-notice-windows.md`.
- Verified on 2026-10-02 against `DESIGN.md`, `web/README.md`, `web/src/components/{ChatLayout,HousekeepingStrip,HomeSetupCard,PinnedFilePanel}.vue`, and the running v1.1.0 candidate's Home view. No visual companion is included: the HTML artifact skill is unavailable in this session.

## Outcome and user value

Actionable notices on Home read as separate, closable windows in the same visual family as a pinned file: a tonal inset surface, bordered title bar, restrained shadow, and a close control at the end of the header. The request composer and recent chats remain usable; notices can be closed without accidentally claiming that the underlying work is done.

## Scope and non-goals

In scope: the operator-action rows and “After this update” tasks in `HousekeepingStrip.vue`, and “Set up this device” in `HomeSetupCard.vue`. Each notice is an individual window, including each update task; no enclosing omnibus notices window. A compact way to reopen closed notices remains on Home. Preserve existing task actions, status/error messages, server-side dismissals, and device-local setup dismissal.

Out of scope: `HomeReviewSummary`'s status rail, recent chat rows, alerts on other routes, file viewing itself, backend API/state changes, and new automatic modal interruptions. This is a presentation and local-open-state change, not a change to when notices are detected or completed.

## Current-state evidence

- **Observed:** `ChatLayout.vue` renders `HousekeepingStrip` and `HomeSetupCard` inline between the Home composer and `HomeRecentChats`, in two mutually exclusive Home branches. The existing `home-rail` contains `HomeReviewSummary` and collapses below the main column by pane width.
- **Observed:** `HousekeepingStrip.vue` owns distinct operator actions and an “After this update” group. Update tasks already have durable **Hide it**, start/resume/review, errors, and focus restoration. `HomeSetupCard.vue` has browser-local **Hide**, and auto-hides after real setup completes.
- **Observed:** `PinnedFilePanel.vue` places its close control last in a `PaneHeader`; `ChatLayout.vue` gives its surrounding tile `--bg2`, a one-pixel border, `--radius-lg`, and a soft shadow. `DESIGN.md` explicitly calls that tile a docked window, not a flat column.
- **Observed:** the v1.1.0 candidate showed an update task and a device-setup notice as unboxed rows on Home. The user requested all Home notices as separate closable windows matching the pinned-file style, before publishing v1.1.0.
- **Follow-up scope:** the user also requested chat-row signals over the right-aligned time (mobile and desktop), one concise extraction label for active memory insight rows, “At a glance” for the Home review rail, and validation of the device setup prompt's recurring check. An installed standalone window is detected by display mode; a separate browser tab cannot reliably see that installation, so keep its explicit per-browser Hide action and clarify the copy.
- **Assumed, pending approval:** “close” means close this presentation for the current browser session, not persistently dismiss the underlying task. This prevents the familiar window X from silently settling or hiding a task across devices.

## Recommended direction

1. Introduce a small reusable **notice-window shell** for Home: header (notice title and final X), content slot, optional accessible status; mirror the pinned tile's tokens and header rhythm without embedding the file viewer or duplicating its document behavior. Keep windows **in the Home flow**, stacked below the composer, not floating over a chat or stealing focus. On a narrow pane each fills the available column with 44px close/action targets; at desktop widths the content remains readable rather than stretching the text across the whole pane.
2. Render each operator action, each update task, and device setup in its own shell. Preserve their existing action handlers and data/status ownership. Keep the group-level “After this update” explanation as context inside each update-task window only if needed; avoid repeating a large heading above the stack.
3. X hides only that window for the current tab/session and workspace, retaining its task in the store. A compact **Notices (N)** control on Home reopens the closed windows; a newly detected notice remains visible by default. Existing **Hide it** (task) and **Hide** (device setup) retain their durable semantics, and their copy should distinguish them from the transient X if both remain visible.
4. Keep the backend untouched. Extract the shell/style and local visibility orchestration at the Home component boundary, with explicit keys stable across workspace switches and update-task revisions. No window should suppress another's action or error state.

## Alternatives and rejected options

- **One notices window:** rejected by the user's choice of separate windows.
- **Blocking modal for every alert:** rejected because it would interrupt the primary chat task and contradict the current non-modal operator-action contract. A pinned file is a docked window, not a modal.
- **X calls the existing persistent Hide action:** rejected as the proposed default because a window close should not mark update work as dismissed or silently persist across devices. This remains an explicit approval question.
- **Reuse `PinnedFilePanel` itself:** rejected; its document-loading/editing and split-view responsibilities are unrelated. Reuse its visual tokens and header conventions.

## Visual review

Review the intended shape against the live pinned file panel: individual title bar + X, indigo inset surface, border and restrained shadow. The first window under the composer is an update task; a second is device setup. On mobile they stack within the Home column with no horizontal overflow. No HTML companion was made because the `html-artifact` skill is unavailable; browser screenshots are the implementation review artifact.

## Decisions and hard-to-reverse bets

| ID | Decision | Rationale | Status |
| --- | --- | --- | --- |
| D-01 | Separate in-flow windows, not a grouped dialog or overlay | Matches the user's separate-window choice while leaving the composer usable | User selected separate windows; exact placement proposed |
| D-02 | Close only changes local presentation; durable Hide remains explicit | Prevents accidental settlement and preserves existing sync/state semantics | Approved |
| D-03 | Reopen affordance remains on Home and names the number closed | An X without recovery would make important work disappear for the session | Approved |
| D-04 | No backend/schema changes | Existing action/task owners remain authoritative | Approved |

## Open questions with recommended defaults

| ID | Question | Recommended default | Status |
| --- | --- | --- | --- |
| Q-01 | Should X persist across reloads? | No: current-tab/session only; all uncompleted notices return on a fresh page load | Resolved: approved |
| Q-02 | Should the existing durable Hide buttons remain? | Yes, with copy clarified if needed; X is only presentation | Resolved: approved |

## Not yet specified (fog of war)

The exact presentation of a newly arriving notice while some are closed should be settled in implementation against the store's refresh behavior; default is to show the new item without reopening previously closed items. No other routes are in scope.

## Feedback and decision log

| ID | Location | Feedback | Decision | Status | Evidence |
| --- | --- | --- | --- | --- | --- |
| F-01 | Release timing | User wants this UI change before v1.1.0 | Pause #907; merge UI work into `develop`, then cut a new candidate | Accepted | User response in release walkthrough |
| F-02 | Scope | “All Home notices” | Include operator actions, update tasks, and device setup; exclude passive review status | Accepted, pending plan approval | User response and `ChatLayout.vue` |
| F-03 | Reference | Match the pinned file panel | Use its window shell language, not its file logic | Accepted, pending plan approval | User response and `PinnedFilePanel.vue` |
| F-04 | Multiplicity | Separate windows | One window per actionable notice | Accepted | User response |
| F-05 | Approval | Approved the specific X/reopen/Hide and in-flow window direction | Implement on develop; #907 closed unmerged | Accepted | User response; #907 |

## Implementation checkpoints

1. **Approve:** resolve Q-01/Q-02 and placement. Exit: user explicitly accepts the plan.
2. **Implement on `develop`:** add the shell, wire per-notice close/reopen state, preserve all existing handlers in `HousekeepingStrip` and `HomeSetupCard`; update `DESIGN.md`'s Home notice description if the approved presentation changes its principles. Exit: all three notice kinds render as independent windows.
3. **Verify:** component tests for close/reopen, refresh/new notice, workspace switch, durable Hide distinction, chat-signal placement, extraction label, standalone setup check on repeat mount, keyboard/focus, narrow layout and touch targets; `mypy ciao`, `pytest -n auto tests/`, `npm test`, `npm run build`, browser inspection at desktop/390px/200% zoom and light/dark themes. Run the Impeccable detector once over changed UI files. Exit: gates and visual check pass.
4. **Ship change into `develop`:** review and merge via the repo's PR gate; close the now-stale release PR #907 without merging, re-run release preparation from updated `develop`, reinstall and walk the new candidate before shipping. Exit: published v1.1.0 contains this UI work, not the old frozen candidate.

## Verification and rollout

The existing v1.1.0 candidate is installed locally and its Linux, macOS and Windows CI jobs passed, but #907 was **closed unmerged**. The new Home notice redesign was visually inspected at desktop and 390px mobile width, at a narrow 640px viewport (200%-zoom equivalent), and in light mode; the browser verified close/reopen and 44px close targets. The frontend suite passed (2020 tests before the final copy/test additions; focused 110 tests after them), and `npm run build` and `mypy ciao` passed. The backend suite passed with `.venv` Python 3.13 (6558 passed, 45 skipped); an earlier system Python 3.11 run failed nine existing Python-version-sensitive tests (`Path.walk`, `shutil.rmtree(onexc)`, and `.md` MIME). A new release PR must be cut from revised `develop` and reviewed/tested as a new fixed point. Never merge #907 just to avoid recutting it.
