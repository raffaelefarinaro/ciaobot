# Ciaobot Core UI Polish and Workbench Plan

## Resume block

- Status: approved
- Current checkpoint: C4 (rebased onto `origin/develop`, installed, verified)
- Next action: none pending; prototype A is fully ported to Today (F-17–F-19) and installed via `ciao-dev-install`
- Blocker: none
- Verified on this slice: `npm run build`, `npm run lint` (0 errors), `npm test` (119 files / 1,557 tests), `npm run test:e2e` (14 tests, incl. new `workbench-layout.spec.ts`), plus dark/light 1440px, 900px and 420px captures (home, mobile drawer, open chat, keyboard focus) against prototype A
- Installed build (2026-09-24): `~/Applications/Ciaobot.app` from this branch's runtime + Tauri build; engine `0.18.1` reachable on :8443, workspace `/Users/raffaelefarinaro/repos/ciao` preserved, desktop shell running, no tracebacks in the boot window
- Branch: `raffaelefarinaro/core-ui-workbench` (two commits on top of `origin/develop` `300f436d`); uncommitted work is limited to transient capture artifacts and a throwaway `web/shot.mjs`
- Implementation repository: `/Users/raffaelefarinaro/repos/ciaobot`
- Generated plan output: `docs/proposals/2026-09-24-core-ui-polish.md`
- Visual companions: `docs/proposals/ciaobot-workbench-prototypes.html` (self-contained interactive comparison; A inspected at 1440px and 420px, B/C inspected at 1440px, and the light theme toggle was exercised)
- Verified on: 2026-09-24 against `PRODUCT.md`, `DESIGN.md`, `docs/ARCHITECTURE.md`, `docs/DEVELOPMENT.md`, `web/README.md`, the current `web/src` implementation, the synthetic PWA fixture, the prior browser captures, and the user-provided UI reference images.

### Rebase notes (2026-09-24, post reka-ui migration)

Upstream had moved 40 commits ahead and migrated the overlays and sidebar menus to `reka-ui` (`ConfirmDialog`, `PromptDialog`, `NewChatPicker`, `FileViewerModal`, `ProjectSidebar`). Resolution rule applied:

- Keep upstream's `reka-ui` focus/menu plumbing and its upstream test suites for those files; drop the hand-rolled `useModalFocus` usage that reka supersedes there.
- Re-apply the workbench pieces the merge would otherwise drop: the sidebar's single global New chat and one workspace scope dropdown (replacing the three per-mode workspace toggles), the `Today` nav label, and the `FileViewerModal` iframe title + coarse-pointer targets.
- `useModalFocus` remains used by `ChatPanel` and `MemoryMapView`, which upstream did not migrate.

This block is the handoff contract. A future agent should read it first, then read the current checkpoint and open questions before editing source.

## Outcome and user value

Ciaobot should feel like one calm place to see **what needs attention, what agents are doing, what changed, and what became durable knowledge**. The pass will make the core journey understandable within seconds and polished enough to use all day without feeling like a collection of admin panels.

The target experience is a provider-neutral **Ciao Workbench**:

```text
Workspace / project navigation  |  Request or active thread  |  Context · Activity · Output
                                  |                              |
                                  |  persistent composer / result |  conditional inspector or file pane
```

The structural vocabulary maps to capabilities that already exist:

- **Request / thread** → existing chat and project hierarchy
- **Run** → existing turn, tool, permission, delegate, and background-run state
- **Output** → existing touched-file cards, pinned files, and file viewer
- **Durable knowledge** → existing archives, proposals, vault review, and Memory Map

This is an evolution of the established Ciao Console identity, not a new product model or a new backend contract.

## Scope and non-goals

### In scope

- Make load, empty, error, stale, retry, and partial states truthful across home, review, automations, files, and project views.
- Establish one accessible interaction language for dialogs, menus, rows, forms, touch targets, focus, and keyboard behavior.
- Rework the global shell and navigation so the active workspace, project, and current task are always clear.
- Turn home into an action-first “jump back in” view with one intake composer, visible attention states, and a compact review/change rail.
- Keep the transcript dominant in chat while adding a conditional, collapsible Context / Activity / Output inspector using existing state.
- Make the project overview summary-first; move the large context editor behind an explicit edit state.
- Make Memory review-first and source-grounded; keep Graph and List as deeper exploration modes.
- Apply the same hierarchy, state, and component language to Automations and Settings.
- Improve dark and light themes, typography, spacing, scroll affordance, mobile layout, zoom, text scaling, reduced motion, and 44px touch targets.
- Update `DESIGN.md`, `web/README.md`, focused tests, browser tests, and relevant fixtures.
- Remove the transient Impeccable live script from tracked `web/index.html` before deterministic test runs and before final delivery.

### Out of scope for this pass

- New backend routes, persistence schemas, provider integrations, or environment variables.
- A new global Inbox, Runs, Artifacts, or Connections navigation system.
- Team collaboration, sharing, comments from other users, or cloud synchronization.
- A full visual reset, new typeface family, glassmorphism, neon treatment, or replacement palette.
- Changes to the native macOS tray, startup screen, or desktop shell.
- A large speculative rewrite of `ChatPanel.vue`, `ProjectSidebar.vue`, or the file viewer.
- Product claims not already supported by `PRODUCT.md` and the capability catalog.

## Current-state evidence

### Observed product and design truth

Observed by reading `PRODUCT.md`, `DESIGN.md`, `web/README.md`, and the current Vue implementation:

- Ciaobot's real differentiator is continuity: workspace → project → chat, followed by archives, reviewable memory, and portable Markdown.
- The incumbent visual identity is coherent: deep indigo surfaces, warm pink orientation color, violet secondary signals, system sans for prose, monospace for technical metadata, compact geometry, and low-shadow hierarchy.
- The product should remain an **Operate** surface. Familiar productivity patterns and efficient scanning outrank decorative expression.
- Host/client state, provider state, permissions, touched files, and memory proposals are product-critical trust signals.
- Browser zoom, font scaling, safe areas, reduced motion, and keyboard access are binding constraints.

### Observed live UI before the prior implementation pass

The following audit was observed in the synthetic browser fixture at 1440×900 and 390×844 in both themes before the completed C6 implementation pass. It explains the original direction; it is not a claim about the current fixture.

- Home has a strong navigation rail but no visible page title or primary intake action; its content is compressed into the top of a very large empty canvas.
- Workspace tabs, project navigation, and the home chat list repeat the same hierarchy in adjacent surfaces rather than each surface adding a distinct job.
- The centered `ciaobot` header consumes the main header position while the actual home heading is visually hidden.
- An empty chat is literally blank except for the composer. During work, status is communicated through a small thinking row and sidebar dots rather than a persistent execution view.
- User turns stretch almost the full content width; there is no calm reading measure or clear distinction between conversation, execution detail, and output.
- Project view begins with four equal statistic cards, an orphaned `created` label, and a permanently expanded context textarea. The most important project summary is visually weaker than editable configuration.
- Memory gives substantial space to token budgets, path-finding instructions, and an empty graph before showing the review outcome that differentiates Ciaobot.
- Settings is a long set of similarly weighted bordered cards with mixed uppercase mono labels and long line lengths.
- Light mode is washed out: the `--fg3` token is approximately 2.86:1 against the light canvas. Dark-mode subtle text is approximately 4.07:1, below the 4.5:1 target for normal text.
- Custom controls below the 44px product minimum include the project close control, Memory segmented controls, theme buttons, and several file/comment actions. The existing browser test only covers `.btn-icon` and `.touch-hit`.
- Mobile navigation is usable but icon-only at the top, the project delete action is prominent, the home screen is mostly unused space, and the chat header does not expose project or host context.

Evidence captures:

- `docs/proposals/ciaobot-ui-audit-home-desktop-clean.png`
- `docs/proposals/ciaobot-ui-audit-home-mobile-clean.png`
- `docs/proposals/ciaobot-ui-audit-chat-active-desktop-clean.png`
- `docs/proposals/ciaobot-ui-audit-chat-active-mobile-clean.png`
- `docs/proposals/ciaobot-ui-audit-project-desktop-clean.png`
- `docs/proposals/ciaobot-ui-audit-project-mobile-clean.png`
- `docs/proposals/ciaobot-ui-audit-memory-desktop-clean.png`
- `docs/proposals/ciaobot-ui-audit-settings-desktop-clean.png`

### Current live state after the prior pass

Observed in the restarted synthetic fixture at 1440×900 and 390×844 after the C6 implementation:

- Home now has a compact prompt bar, New opens the shared project picker, and the Home review summary keeps memory proposals, knowledge upkeep, and automations visible.
- The sidebar now has one top-level workspace scope selector, labeled global navigation, and contextual Memory content driven by the main pane's Review/Map mode.
- Chat headers show a clickable project context control without repeating the workspace, and empty chats expose a distinct project-knowledge action.
- The current composition is functional and tested, but it is still more card/list oriented than the reference screenshots; this follow-up prototype tests a quieter workbench composition before further production changes.

### Observed implementation risks

Observed by reading the current components, stores, and tests:

1. `stores/vaultReview.ts` and `VaultReviewPanel.vue` can turn a failed first load into “Nothing to revisit.”
2. `stores/tasks.ts`, `ChatLayout.vue`, `ProjectSidebar.vue`, and `SchedulePanel.vue` do not share a reliable initial schedule load/error contract; a failed fetch can appear as an empty automations list.
3. `MemoryMapView.vue` hides its detail panel below 900px, leaving selection without an obvious preview or open-file path.
4. `ConfirmDialog.vue` accepts on window-level Enter even when Cancel has focus. Other dialogs do not consistently contain focus, inert the background, or restore the opener.
5. Several project, file, archive, and memory rows are click-only `<div>` elements; context menus are not consistently keyboard-operable.
6. Coarse-pointer target sizing is viewport-dependent and does not cover all custom controls.
7. Scrollbars are globally hidden, removing an important cue in files, tables, tabs, and long settings panes.
8. `FileViewerModal.vue` and `PinnedFilePanel.vue` duplicate substantial rendering/comment behavior; some styles/state appear unused, while History/Diff documentation and implementation need reconciliation before removal.
9. `ChatLayout.vue`, `ChatPanel.vue`, and `ProjectSidebar.vue` are very large. The repository already documents incremental extraction boundaries; a wholesale rewrite would be riskier than composition-preserving extractions.
10. `web/README.md` still describes a primarily monospace type scale, while `App.vue` and `DESIGN.md` define the current hybrid type system.
11. `DESIGN.md` says working precedes unread on home, while `HomeRecentChats.vue` and `lib/homeLanes.ts` currently put unread before working. This must become one explicit contract.

### Baseline verification

Observed on 2026-09-24:

- `cd web && npm run build` — passed.
- `cd web && npm test` — 112 test files and 1,460 tests passed.
- `cd web && npx playwright test` against the generated build with the transient live script removed — 10 tests passed.
- `cd web && npm run lint` — 0 errors, 12 warnings.
- `npm run test:e2e` with the tracked live script present produced 8 passes and 2 failures because the injected Impeccable bar intercepted the composer; the same tests pass when the generated build omits that script.

The temporary live injection in `web/index.html` is local tooling state, not product behavior, and must not ship.

### External evidence and interpretation

Directly documented patterns:

- Anthropic merged Cowork and chat because forcing users to decide where a task belonged was frustrating; projects carry instructions, context, schedules, scoped memory, and the same conversation can produce multiple editable artifacts. Source: https://claude.com/blog/cowork-is-now-claude
- ChatGPT Work exposes clear local/cloud boundaries, uses approved files/tools, supports progress, questions, direction changes, approvals, and reviewable outputs. Source: https://learn.chatgpt.com/docs/get-started-with-work
- Gemini Spark separates Progress, Files, Schedules, and Skills & apps in a work panel and exposes takeover/recovery controls. Source: https://support.google.com/gemini/answer/17094507?hl=en
- Notion Custom Agents keeps Chat, Activity, and Settings as distinct durable views; Activity exposes triggers, actions, failures, version history, and reversible runs. Source: https://www.notion.com/help/custom-agents
- Microsoft Copilot Pages keeps chat and a durable editable artifact visible side by side. Source: https://support.microsoft.com/en-us/microsoft-365-copilot/get-started-with-microsoft-365-copilot-pages

Recommended interpretation for Ciaobot:

- Do not add a Chat / Work mode switch. Keep one request entry and ask only for boundaries that change behavior, such as project, provider, permissions, or plan/read-only mode.
- Do not present “local” as a privacy claim. Show **Host / Client** and **Provider** separately, while preserving the existing privacy copy about provider egress.
- Make execution state inspectable without turning the transcript into an activity log.
- Keep reasoning, execution, and output distinct without creating three new top-level products.
- Keep durable, source-grounded memory visible as the outcome of work rather than a hidden automation.

## Follow-up prototype gate (current)

### User direction captured on 2026-09-24

The user supplied six reference screenshots and asked for a new design pass before more production implementation. The references show a quieter desktop workbench language: a persistent left rail, a nearly empty central canvas for the empty state, a composer anchored to the bottom, compact model/project controls, and a high-contrast dark or light surface. The references do not show Ciaobot's differentiator: reviewable memory, workspace-scoped projects, archives, and durable knowledge.

This follow-up therefore borrows **spatial hierarchy and interaction restraint**, not the competitors' product model or palette. The design question is: **how can a Ciao chat feel as calm and immediate as the references while making memory a visible part of the work rather than a hidden settings page?**

### Prototype set for review

The interactive companion at `docs/proposals/ciaobot-workbench-prototypes.html` presents three deliberately different compositions. They are comparison prototypes, not three proposed product modes.

1. **A — Quiet Workbench (recommended direction)**
   - Graphite/indigo shell with the existing pink accent used sparingly.
   - Left rail: workspace selector, Today / Memory / Automations / Settings, project tree, and recent chats.
   - Main empty state: one large command prompt, a small project/model context strip, and a bottom composer.
   - Wide screens add a quiet **Memory pulse** rail: pending proposals, notes to revisit, and active automations.
   - This is the closest synthesis of the references and Ciaobot's actual hierarchy.

2. **B — Memory-first Home**
   - The home canvas is a daily briefing: memory changes and active work lead; the composer is a persistent bottom action.
   - Useful if the user's dominant daily need is orientation and review rather than starting a new thread.
   - Risk: makes the product feel like a dashboard and pushes chat below the fold.

3. **C — Chat-first Focus**
   - The transcript and composer dominate immediately; projects and memory are contextual drawers.
   - Useful for frequent long-running project work.
   - Risk: hides the memory outcome and makes Home feel like an empty chat rather than a workbench.

The recommendation is **A**, with B's review summary and C's focused transcript as responsive/state variants rather than competing navigation products.

### Interaction thesis to validate

- Workspace is selected once at the top of the rail; `1`–`9` remain fast scope switches.
- `New` always opens the existing shared project picker; project-local New preselects the current project.
- The composer exposes project and model as compact controls, not a second navigation system.
- A wide Memory pulse can open the existing Review/Map routes; it does not duplicate Memory logic.
- Chat keeps a readable reading measure, a bottom composer, and a conditional Context / Activity / Output inspector.
- On narrow screens the rail becomes a drawer, the Memory pulse becomes a compact strip, and secondary actions move into overflow.
- Empty, loading, stale, error, and running states must be visible in the prototypes as first-class UI, not placeholder copy.

### Prototype review questions

- Does the workspace/project/chat hierarchy read immediately without repeating the workspace name in every surface?
- Is the Memory pulse useful glanceable orientation, or does it compete with the composer?
- Should the default Home state be the large command canvas (A), the daily briefing (B), or the focused chat (C)?
- Does the reference-inspired darker surface still feel like Ciaobot when the pink accent is used only for current state and primary action?
- Are model, project, and context controls discoverable without making the composer look like a settings form?

### Production areas held behind approval

No new production source edits should be made from this follow-up until the user selects a direction. After approval, the likely implementation surface is:

- `web/src/components/ChatLayout.vue`
- `web/src/components/ProjectSidebar.vue`
- `web/src/components/HomeIntake.vue`
- `web/src/components/HomeReviewSummary.vue`
- `web/src/components/HomeRecentChats.vue`
- `web/src/components/ChatPanel.vue`
- `web/src/components/PaneHeader.vue`
- `web/src/components/MemoryMapView.vue`
- `web/src/components/SchedulePanel.vue`
- `web/src/App.vue` and `DESIGN.md`

The existing shared `NewChatPicker`, stores, and focus primitives should be reused rather than replaced.

## Recommended direction

### 1. Make state truth a design requirement

Before cosmetic changes, align state contracts across the core journey:

- first load
- successful empty
- refresh over existing rows
- stale refresh failure
- action failure
- partial result
- retry/resume/recovery

Apply the model already proven by `ProposalReviewPanel.vue` to vault review, global automations, and any touched core list.

### 2. Evolve the shell, do not replace it

Primary files:

- `web/src/App.vue`
- `web/src/components/ChatLayout.vue`
- `web/src/components/ProjectSidebar.vue`
- `web/src/components/TabBar.vue`

Direction:

- Put the Ciaobot wordmark in the sidebar identity area; use the main header for the current context (`Today`, project, chat, Memory, Automations, or Settings).
- Replace the ambiguous icon-first global row with clear labeled navigation: **Today**, **Automations**, **Memory**, and **Settings**.
- Keep attention badges on their destination icons/labels.
- Replace the large workspace tab strip with a compact workspace switcher that preserves visible `1`–`9` shortcuts and ordered switching.
- Keep the project/chat tree as the navigational home for the selected workspace.
- On mobile, retain the drawer but give it the same labels, hierarchy, focus behavior, and 44px targets as desktop.
- Remove the normal-app CRT grain or reduce it to an imperceptible texture; keep the terminal treatment in startup/native surfaces where it carries meaning.

### 3. Make home action-first and outcome-aware

Primary files:

- `web/src/components/HomeRecentChats.vue`
- `web/src/components/ChatLayout.vue`
- `web/src/lib/homeLanes.ts`
- `web/src/lib/__tests__/homeLanes.test.ts`

Direction:

- Show a visible **Jump back in** page title and one intake composer. It starts a normal chat/run in the selected workspace and chosen project; it is not a new inbox or persisted object.
- Place attention ahead of recency: **Needs you**, **Working**, **Unread**, then **Earlier** (rename “quiet” in user-facing copy).
- On wide screens, use the reclaimed horizontal space for a compact right rail showing memory proposals, review candidates, and the next relevant automation result. On narrow screens, place this review summary above the lanes.
- Keep shape, label, and weight ahead of color. Give “Needs you” one unmistakable treatment and reduce competing pink outlines.
- Preserve arrow-key navigation and the current lane ordering tests after making the ordering contract explicit in `DESIGN.md`.

### 4. Keep chat dominant, with a conditional work inspector

Primary files:

- `web/src/components/ChatPanel.vue`
- `web/src/components/PinnedFilePanel.vue`
- `web/src/components/ChatTurnActivity.vue`
- `web/src/components/chatTrace.css`
- `web/src/components/ChatLayout.vue`

Direction:

- Keep the transcript as the primary surface and the composer persistently reachable.
- Constrain conversation prose to a comfortable reading measure; let metadata and outputs use a wider but controlled grid.
- Add a meaningful empty state that explains the selected project context and offers the first action.
- Reuse current activity, permission, touched-file, background-run, delegate, and pinned-file state in a conditional inspector with three tabs:
  - **Context** — project instructions and pinned/source material
  - **Activity** — current state, meaningful steps, waits, and recoverable errors
  - **Output** — created/modified files, previews, and pinned artifacts
- Do not force a permanent fourth column. The inspector appears when there is useful state or the user opens it; the existing file split remains available.
- On mobile, open the inspector as a focus-contained sheet and return focus to its trigger.
- Show provider and host/client context in the chat header without implying that provider inference is local.

### 5. Make Project summary-first

Primary files:

- `web/src/components/ProjectView.vue`
- `web/src/components/PinnedFilePanel.vue`

Direction:

- Replace the four equal stat cards with one compact status strip and a clear project summary.
- Show active work, outputs/files, open decisions, and automations before editable context.
- Render project context read-only by default with an explicit **Edit context** action; use the existing textarea in a focused edit state or sheet.
- Move Delete into the project overflow menu on desktop and a clearly labeled destructive overflow action on mobile.
- Omit incomplete metadata such as a bare `created` label rather than showing an empty fact.
- Reuse the shared file/output treatment rather than inventing a project-only card system.

### 6. Make Memory the visible outcome

Primary files:

- `web/src/components/MemoryMapView.vue`
- `web/src/components/ProposalReviewPanel.vue`
- `web/src/components/VaultReviewPanel.vue`
- `web/src/components/ProposalHistoryList.vue`
- `web/src/stores/memoryMap.ts`
- `web/src/stores/proposals.ts`
- `web/src/stores/vaultReview.ts`

Direction:

- Keep the route and primary navigation label **Memory** for continuity; use **Review** and **Map** as the two dominant modes.
- Make proposals, retirement candidates, trash, and history peers in Review.
- Put source, destination, freshness, and accept/dismiss consequence before technical budget details.
- Move token budgets and path-finder internals behind a compact **Memory details** disclosure.
- On narrow screens, open selected-note detail in a focus-contained sheet or the file viewer; never hide the only open/preview action.
- Use accurate empty, error, stale, and filtered-empty states.

### 7. Apply the same system to Automations and Settings

Primary files:

- `web/src/components/SchedulePanel.vue`
- `web/src/stores/tasks.ts`
- `web/src/components/SettingsView.vue`
- `web/src/components/settings/`
- `web/src/components/settings/settingsPanels.css`

Direction:

- Use the same visible page title, contextual header, section spacing, status vocabulary, and shared controls.
- Present automations as inspectable work: trigger, target, next run, last result, attention state, and recovery action.
- In Settings, group dense functionality under clear headings and progressive disclosure; do not make every form another equal-weight card.
- Restore visible thin scrollbars or equivalent scroll cues in long panes, tab strips, file surfaces, and wide tables.
- Use native buttons/links for rows and one shared context-menu/focus contract.

### 8. Refine the visual system without changing its identity

- Preserve the existing palette, workspace accent presets, sans/mono roles, low-shadow geometry, and terminal prompt signature.
- Increase normal and subtle text contrast in both themes; do not use failing tertiary text for meaningful metadata.
- Use monospace and uppercase tracking only for IDs, commands, status tokens, timestamps, and technical diagnostics.
- Use cards for repeated items, overlays, and bounded tools; use unframed sections and dividers for page composition.
- Reserve pink for the current location, focus, progress, and the single most important action in a view.
- Keep visible geometry compact and consistent; avoid decorative gradients, glass, glow, and oversized rounding.
- Use 150–220ms transitions only for state, feedback, disclosure, and overlay changes; respect reduced motion.

## Alternatives and rejected options

### Cosmetic token polish only

Rejected because it would preserve false-empty states, keyboard gaps, duplicated navigation, and the weak relationship between chat, execution, files, and memory.

### Clone Claude Cowork or ChatGPT Work

Rejected because their providers, cloud/local model, account constraints, and product scope differ. Ciaobot should borrow the unified entry, project context, reviewable output, and execution visibility—not their navigation labels or visual identity.

### Add top-level Inbox, Runs, Artifacts, and Connections

Deferred. Existing chats, projects, automations, Memory, pinned files, and review queues can express the model with less fragmentation. New top-level destinations would increase cognitive load and require substantial backend/product work.

### Permanent four-column IDE/workbench

Rejected. It would shrink the transcript and file surfaces, intensify the current density problem, and behave poorly on tablets and phones. The work inspector must be conditional and collapsible.

### Full visual reset

Rejected by the user’s selected direction. The existing indigo/pink identity is product-specific and worth evolving.

## Visual review

The current follow-up uses `docs/proposals/ciaobot-workbench-prototypes.html` as an interactive comparison surface. It is grounded in the real Ciao labels and the user-provided reference screenshots, and it is deliberately separate from production source. The Markdown plan remains canonical; the HTML exists to answer the spatial and interaction question.

The prototype must be reviewed at roughly 420px width and at a wide desktop width. The review should cover the workspace menu, project picker, model menu, Memory pulse, chat context, empty state, and narrow drawer behavior. After approval, implementation review should use one bounded visual round at both 1440×900 and 390×844 in dark and light themes, covering home, active chat, project, Memory review, Automations, and Settings. A second confirming round is allowed only for defects found in the first.

## Decisions and hard-to-reverse bets

| ID | Decision | Rationale | Status |
| --- | --- | --- | --- |
| D-01 | Use one Ciao Workbench model: request → run → output → durable knowledge | Connects existing capabilities without inventing a new backend model | Approved |
| D-02 | Do not add a Chat / Work toggle | Claude removed this choice because of user friction; Ciaobot’s chat is already agentic | Approved |
| D-03 | Keep the transcript dominant; make execution context conditional | Preserves focus and reading space while making trust state inspectable | Approved |
| D-04 | Treat Project as the context envelope and Chat as the reasoning thread | This maps directly to the existing workspace → project → chat hierarchy | Approved |
| D-05 | Make Memory review-first and source-grounded | Memory is Ciaobot’s differentiator and product trust mechanism | Approved |
| D-06 | Fix state truth and interaction primitives before visual polish | False empty states and keyboard defects are product bugs, not styling issues | Approved |
| D-07 | Preserve palette, type roles, geometry, and terminal cues; evolve composition | The identity is coherent and the user selected evolution rather than replacement | Approved |
| D-08 | Keep the first pass frontend-only | Existing APIs can support the direction; avoid expensive schema churn | Approved |
| D-09 | Remove transient live injection before deterministic tests/final | It currently intercepts pointer input and contaminates the browser suite | Approved |
| D-10 | Use a prototype gate before the next production composition pass | The user explicitly wants to compare spatial direction before more implementation | Approved |
| D-11 | Keep the reference-inspired shell subordinate to Ciao's workspace → project → chat → memory hierarchy | A generic chat clone would hide the product differentiator and duplicate navigation | Approved |
| D-12 | Use one recommended Quiet Workbench composition with variants, not three permanent modes | Prevents prototype comparison from becoming product fragmentation | Approved |
| D-13 | Adopt prototype A — Quiet Workbench — as the production composition | User selected A explicitly after reviewing the comparison | Approved |

## Open questions with recommended defaults

| ID | Question | Recommended default | Status |
| --- | --- | --- | --- |
| Q-01 | Should home gain a real intake composer? | Yes; it starts a normal chat in the selected workspace/project and creates no new persisted object | Accepted; default approved |
| Q-02 | Should the main navigation item be renamed from Chats to Today? | Yes; the route is a prioritized home view, not a complete chat archive | Accepted; default approved |
| Q-03 | Should Memory be renamed Knowledge? | No; keep Memory, add clear Review and Map modes | Accepted; default approved |
| Q-04 | Should the chat inspector be persistent? | No; show it when useful or explicitly opened, with a mobile sheet | Accepted; default approved |
| Q-05 | Should CRT grain leave the normal application? | Yes; remove or nearly eliminate it there, while preserving startup/native terminal surfaces | Accepted; default approved |
| Q-06 | Should the first pass add new backend run-summary data? | No; compose existing state first and record any concrete data gap instead | Accepted; default approved |
| Q-07 | Which prototype should anchor the next implementation pass? | Start with A — Quiet Workbench — and borrow B/C only as state/layout variants | Resolved: A |
| Q-08 | Should the wide Memory pulse be persistent or available on demand? | Persistent on wide screens when it has useful state; collapsed behind a summary control when empty or narrow | Open |
| Q-09 | How close should the visual surface move from indigo toward the references' near-black graphite? | Keep the indigo identity, but reduce chroma and contrast in the canvas/sidebar so the pink remains the orientation signal | Open |
| Q-10 | Should model selection be visible on the empty Home composer? | Yes, as a compact secondary control; provider/model details remain truthful and provider-neutral | Open |

## Not yet specified (fog of war)

- The exact breakpoint where the desktop work inspector becomes a mobile sheet.
- Whether a concise home review rail can rely entirely on current proposal, review, and automation stores or exposes a meaningful stale-data combination.
- How much of the duplicated home markup should be extracted in the same checkpoint as the visual change versus immediately after it.
- Which file-viewer History/Diff styles are live behavior versus dead residue; this must be reconciled before cleanup.
- Whether a direct note-capture action belongs in a later pass after the chat intake composer proves the unified-entry model.

Resolution after implementation:

- The Work inspector is a right-side drawer on wider panes and a bottom sheet at 600px and below.
- The home review summary reads the existing proposal, retirement, and schedule stores; it distinguishes current-snapshot counts from load failures and adds no fetch or persistence contract.
- The two existing home shell branches stay in place, but shared `HomeIntake` and `HomeReviewSummary` components keep the duplicated markup bounded. No speculative shell extraction was needed.
- File-viewer History/Diff cleanup and direct note capture remain deferred product work, not defects introduced by this pass.

## Feedback and decision log

| ID | Location | Feedback | Decision | Status | Evidence |
| --- | --- | --- | --- | --- | --- |
| F-01 | Whole PWA | “I got many comments that the UI is not the best.” | Audit hierarchy, visual craft, workflow efficiency, and identity together | Accepted | User request; live/code audit |
| F-02 | Direction | “I want to have a polish UI that makes sense.” | Produce one coherent workbench system, not isolated screen makeovers | Accepted | User request |
| F-03 | Research | Check Claude Cowork and ChatGPT Work, plus other relevant solutions | Use current primary sources and borrow structural patterns only | Accepted | Official sources listed above |
| F-04 | Scope | User selected hierarchy, visual craft, workflow efficiency, and identity | Include all four in the core-journey pass | Accepted | Discovery response |
| F-05 | Visual world | User selected “Evolve the system” | Preserve the incumbent identity; replace weak composition and interaction patterns | Accepted | Discovery response |
| F-06 | Breadth | User selected “Core journey, system-wide” | Cover home, chat, project, files, Memory, Automations, Settings, and shared foundations | Accepted | Discovery response |
| F-07 | Approval gate | User selected “Approve plan” | Implement the recommended direction and all stated defaults in tested batches | Accepted | Plan approval response |
| F-08 | Memory landing | The final audit showed the new Review/Map hierarchy still opened on Graph | Make Review the first-visit landing state while remembering a deliberate Graph/List choice | Accepted | Store/component change, unit test, desktop capture, mobile browser test |
| F-09 | Independent diff review | Review found workspace-scope, stale-state, keyboard, menu, focus, and draft-preservation gaps | Resolve all P1/P2 findings and the bounded concurrency/test-confidence gaps before closing | Accepted | Source fixes, focused regressions, final full gates, review screenshots |
| F-10 | Follow-up prototype gate | User supplied dark/light workbench references and asked to show prototypes before building all of it | Stop new production edits; create a self-contained three-variant prototype and request direction approval | Open | User message in current turn; `ciaobot-workbench-prototypes.html` pending |
| F-11 | Reference interpretation | References show model placement, bottom composer, sparse navigation, and workspace selection, but omit memory | Borrow the spatial/interaction patterns; make Memory pulse a first-class Ciao-specific outcome rather than copying the reference product | Proposed | User-provided screenshots; `PRODUCT.md`; `DESIGN.md` |
| F-12 | Prototype recommendation | Three compositions are being compared rather than silently mixing them | Recommend Quiet Workbench (A), with Memory-first content and Chat-focus states as variants | Proposed | Follow-up prototype gate above |
| F-13 | Prototype verification | A must remain readable at panel width and C must keep work details inspectable | Verified A at 420px, all three at desktop width, theme toggle, workspace menu, picker, inspector tabs, and send interaction | Accepted | `ciaobot-workbench-prototypes.html`; local browser checks |
| F-14 | Direction choice | The user selected prototype A, then confirmed “A / option A” | Translate Quiet Workbench into production, keeping the Memory pulse as the Ciao-specific addition | Implemented (Today slice) | User answer; `HomeIntake.vue`, `ChatLayout.vue` workbench changes; green build/lint/test/e2e |
| F-15 | Today command canvas | A's big prompt needs a working project control and a clear primary action | Add a project chip that only remembers a pick, a prompt textarea, and one New button with honest helper copy | Implemented | `HomeIntake.vue`; `HomeIntake.test.ts` (6 tests); 1440px/420px dark+light capture |
| F-16 | Today layout ownership | Review summary must not compete with the composer | `.home-workbench` two-column grid with `.home-rail` sticky on wide panes, single column under 980px | Implemented | `ChatLayout.vue`; `DESIGN.md`, `web/README.md`, `docs/ARCHITECTURE.md` updated |
| F-17 | Prototype A full port | Today shipped A's structure but kept card styling, a small composer and a crowded sidebar header | Port hero scale, command surface, memory-pulse rows, flat Continue rows, stacked sidebar top and Today crumbs; omit Add context / mic / thinking / Updates (no real handler on an empty home) and show only the workspace default provider, read-only | Implemented | `HomeIntake.vue`, `HomeReviewSummary.vue`, `HomeRecentChats.vue`, `ProjectSidebar.vue`, `HostStatusPill.vue`, `ChatLayout.vue`, `App.vue`; `workbench-a-port-*.png` |
| F-18 | Honest composer hint | The old hint claimed "Enter sends" but bare Enter inserted a newline | Wire ⌘/Ctrl+Enter (chat's chord) and say so; bare Enter stays a newline | Implemented | `HomeIntake.test.ts` |
| F-19 | Sidebar header at 340px | Workspace scope, four nav icons and New chat shared one 61px row and clipped | Stack them; destinations become a labelled list | Implemented | `e2e/specs/workbench-layout.spec.ts` |

## Implementation checkpoints

### C0. Start or resume — complete

- Read the Resume block and current checkpoint.
- Read project/product/design/development guidance.
- Record the dirty worktree and preserve user-owned files.

Exit evidence: this plan names the repository, current state, and existing uncommitted files.

### C1. Ground the plan — complete

- Map the shell, routes, stores, core components, states, and tests.
- Inspect the running PWA at desktop/mobile and dark/light.
- Record observed defects and positive foundations.

Exit evidence: current-state claims above are independently verifiable from repository paths and captures.

### C2. Set direction — complete

- Choose one Ciao Workbench direction.
- Record rejected alternatives and hard-to-reverse bets.
- Preserve product truth and avoid backend/schema expansion.

Exit evidence: the plan is executable in one direction.

### C3. Build the review artifact — complete

- Write this canonical Markdown plan.
- Link the browser evidence.
- Skip an HTML companion because the required authoring skill is unavailable.

Exit evidence: the plan and evidence paths are durable in the repository.

### C4. Review the artifact — complete

- Inspect this plan against the live and code evidence.
- Confirm the direction, scope, defaults, and files/areas to be touched.
- Record that the desktop review pane was unavailable in this harness; the canonical file path is the review surface.

Exit evidence: the user can approve one explicit direction without relying on chat history.

### C5. Get approval — complete

- Capture approval or requested changes in the feedback log.
- Mark the plan `approved` only after explicit acceptance.
- Do not edit product source before that point.

Exit evidence: the Resume block and feedback log reflect the user’s decision.

### C6. Implement — complete

Work in small, testable batches.

#### C6.1 State truth

- Fix vault-review first-load/error/stale behavior in `stores/vaultReview.ts` and `VaultReviewPanel.vue`.
- Add a shared schedule load/error/stale contract in `stores/tasks.ts` and consume it consistently from `ChatLayout.vue`, `ProjectSidebar.vue`, `SchedulePanel.vue`, and `ProjectView.vue`.
- Add focused regression tests before visual migration.

#### C6.2 Interaction primitives

- Create a shared modal/focus-containment contract based on the strongest existing patterns in `NewChatPicker.vue`.
- Fix `ConfirmDialog.vue` Enter behavior and focus restoration.
- Apply the primitive to `PromptDialog.vue` and the file-viewer overlay without changing file behavior.
- Add a shared keyboard context-menu contract and native row controls in Project, Sidebar, and Memory views.
- Enforce coarse-pointer 44px targets globally and extend browser coverage beyond `.btn-icon`/`.touch-hit`.
- Remove the stale FileViewer Escape listener during cleanup.

#### C6.3 Narrow workflows and states

- Give Memory selection a focus-contained mobile detail sheet or direct file-opening fallback.
- Repair mobile review, file/comment, toast, form-label, and iframe-title semantics.
- Restore visible scroll affordance for focusable data regions and long panes.

#### C6.4 Shell and home

- Implement the labeled global navigation and compact workspace switcher.
- Move brand/context hierarchy to the correct header locations.
- Add the home intake composer, visible page title, revised lane language, and compact review/change rail.
- Align `DESIGN.md`, `lib/homeLanes.ts`, and tests on one priority order.

#### C6.5 Chat and project

- Refine transcript measure, header hierarchy, empty state, and composer context.
- Add the conditional Context / Activity / Output inspector using existing state.
- Rework Project into a summary-first overview with progressive context editing and overflow-owned destructive actions.

#### C6.6 Memory, Automations, and Settings

- Make Memory Review and Map hierarchy source-grounded and review-first.
- Move technical budget/path details behind disclosure.
- Apply the shared page/header/section system to Automations and Settings.
- Normalize status language, empty states, and recovery actions.

#### C6.7 Incremental extraction and documentation

- Keep the duplicated home shell branches bounded with shared `HomeIntake` and `HomeReviewSummary` components; do not extract the shell speculatively after both branches are covered.
- Extract only the shared file/metadata/comment pieces proven necessary by the work; do not rewrite the large components wholesale.
- Update `DESIGN.md`, `web/README.md`, and any affected architecture/development notes.
- Run the Impeccable mechanical detector once at the end, not during concept selection.

### C7. Verify — complete

- Run focused tests after each batch.
- Run the full frontend test/build/browser gates.
- Inspect the live PWA in dark/light at desktop/mobile, maximum font scale, browser zoom, keyboard-only operation, reduced motion, and narrow touch widths.
- Record failures, stale captures, skipped device-only checks, and any unimplemented recommendation.

### C8. Close or hand off — complete

- Set status to `complete`, `deferred`, or `blocked`.
- Record the verified commit/worktree state.
- Leave a concrete next action only if work remains.

## Follow-up checkpoint ledger

The checkpoints below are the active gate for the new visual direction. The earlier C6–C8 record remains historical evidence for the already-implemented functional pass.

### C0. Prototype direction — complete

- Re-read the user references, `PRODUCT.md`, `DESIGN.md`, the live fixture, and the current shell.
- Keep production source unchanged while the direction is selected.

Exit evidence: this plan and the prototype companion are reviewable without relying on chat history.

### C1. Prototype comparison — complete

- Build the self-contained HTML companion with A/B/C compositions and real Ciao labels.
- Include workspace menu, project picker, model menu, Memory pulse, chat context, and narrow layout behavior.

Exit evidence: the reviewer can change the major composition and inspect the primary interactions without a build.

### C2. Direction approval

- User selects A, B, or C and records any requested changes in the feedback log.
- Resolve Q-07 through Q-10 or explicitly defer them.

Exit evidence: the Resume block is marked `approved` only after a direction is accepted.

### C3. Production translation — Today slice complete

- Mapped prototype A's two-column workbench onto the existing Today surface: `HomeIntake.vue` is now a command canvas (project chip, prompt textarea, single forward action) and `HomeReviewSummary.vue` renders in a `.home-workbench` side rail on wide panes, stacking below the request column under a 980px container width.
- The project chip reuses the shared `NewChatPicker` and only remembers the chosen project; it never creates a chat and never clears an unsent draft. `New`/Enter still start the chat and send an existing prompt.
- Reused `NewChatPicker`, existing stores, and the current lane/review contracts; no new API, schema, or environment variable.

Exit evidence: source changes are traceable to the approved prototype and remain frontend-only; focused and full frontend gates are green.

### C4. Bounded visual verification — Today slice verified

- Inspect the selected direction at wide and narrow widths, dark and light, with keyboard and touch states.
- Fix defects in one batch, then stop.

Exit evidence: the running fixture shows the approved composition and the focused/full gates are rerun.

## Implementation and verification record

### Delivered

- Truthful shared schedule and retirement states: separate loading, load error, loaded/current-snapshot, and stale-refresh behavior; failed refreshes retain the last successful rows.
- Shared modal focus behavior across Confirm, Prompt, File Viewer, chat Work details, and narrow Memory detail, including Escape capture, inert background content, focus containment, and opener restoration.
- Native/keyboard-operable project and memory rows, project/chat context menus with menu roles and arrow/Home/End/Escape behavior, coarse-pointer 44px targets, associated automation labels, iframe naming, and error-toast alert semantics.
- Today navigation and page hierarchy, home outcome intake with per-workspace draft preservation, Needs You → Working → Unread → Earlier lanes, and a workspace-scoped review summary that distinguishes checking, current, stale, empty, and failed memory/automation state.
- Chat empty-state hierarchy, a constrained transcript measure, and conditional Context / Activity / Output Work details without implying local inference.
- Summary-first Project with progressive context editing and overflow-owned destructive actions.
- Memory as a review-first landing surface, with Map/List one action away, technical guide budgets behind disclosure, and a focus-contained mobile note sheet.
- Independent-review hardening: keyboard Work tabs and Project menus, initially-active modal focus, project-identity edit reset, wide-touch action visibility, starter-draft protection, output deduplication, retirement load initialization/error truth, mutation snapshot ordering, and workspace-safe home intake/review counts.
- Updated `DESIGN.md`, `web/README.md`, and `docs/ARCHITECTURE.md`; no backend, schema, provider, desktop, or environment-variable change.

### Automated gates — 2026-09-24

- `npm test`: **116 files / 1,486 tests passed**.
- `npm run build`: **`vue-tsc --noEmit` and Vite production build passed**.
- `npm run lint`: **0 errors / 12 pre-existing warnings**; no new lint category was introduced.
- `npm run test:e2e`: **12 Playwright tests passed**, including 390px layout and form containment, wide-touch action visibility, all-visible-control touch measurement, Memory note sheet, keyboard navigation, workspace shortcuts, 200% equivalent zoom, and 1.5× text scale.
- `git diff --check`: passed.
- Impeccable layout detector: **0 findings** across `web/src`.
- Independent read-only diff review: **0 P0; all P1/P2 findings resolved** with focused regressions.

### Visual and interaction evidence

- Inspected desktop 1440×900 and mobile 390×844 in dark and light themes.
- Inspected Home, empty Chat, Work details desktop/mobile, Project, Memory Review, Memory Map, Automations, Settings desktop/mobile, project menu, 200% equivalent zoom, and 1.5× text scale.
- Confirmed no document-level horizontal overflow at 390px, no clipped primary navigation labels or intake controls, and visible inspector/menu focus states.
- Device-only native safe-area and hardware-keyboard checks remain manual QA concerns; the existing viewport/safe-area implementation and automated narrow-viewport coverage remain in place.
- Injected running/failed/stale-refresh permutations were verified through focused state tests rather than separate live captures; no unsupported claim is made that the synthetic screenshot run exercised a real provider or backend failure.

## Verification and rollout

### Required automated gates

From `web/`:

```bash
npm test
npm run build
npm run lint
npm run test:e2e
```

`npm run lint` is advisory in CI but should be reviewed. Fix warnings in touched files; do not expand scope solely to unrelated warnings.

### Required focused coverage

- Vault review: rejected first load, retry, stale refresh over existing rows, workspace switch.
- Automations: rejected first load, retry, stale rows, sidebar and page consistency.
- Dialogs: Cancel + Enter, focus containment, Escape, background inerting, opener restoration.
- Menus/rows: keyboard traversal and activation.
- Memory: narrow selection opens detail/file; focus returns to trigger.
- Touch: all visible coarse-pointer controls meet 44×44 CSS px or an equivalent spacing/target exception.
- Home: lane order, new intake action, workspace shortcuts, and review summary.
- Chat: empty state, active state, inspector tabs, output/file behavior, composer at mobile and maximum font scale.
- Project: progressive context editing, mobile overflow actions, loading/error/empty states.
- Settings/Automations: shared controls, status hierarchy, and scroll access.

### Required visual verification

Capture and inspect the same named states in both themes:

- home with attention, running, unread, and earlier work
- empty home
- empty chat and active chat
- chat with pinned output/context
- project summary and context edit state
- Memory Review and Map/empty state
- Automations with healthy, running, failed, and stale-refresh states
- Settings at desktop and mobile

Verify:

- 390px width and a wide touch viewport
- 200% browser zoom and maximum in-app text scale
- keyboard-only navigation and visible focus
- screen-reader names/states for changed controls
- reduced motion
- safe areas and keyboard-open behavior where manually testable
- no horizontal page overflow
- visible scroll cues for bounded horizontal regions

### Rollout

- Do not restart the running Ciaobot service from inside a PWA chat.
- Use the normal Settings → Deploy workflow after the build.
- Keep the change frontend-only unless implementation proves a backend contract is necessary; any such proof pauses the batch for explicit scope review.
- Do not commit the Impeccable live token/script, `.impeccable` transient state, generated screenshots used only for local review, or unrelated existing worktree files unless explicitly intended.
