# PWA frontend

Vue 3 + Vite + Pinia + TypeScript. Built output goes to `ciao/web/static/`, served by the same Starlette server as the API. See `../README.md` for the repo-wide layout and `../PWA_API.md` for backend routes.

The file viewer is Vue-first. Text, Markdown, CSV, PDF/PPTX (via a binary preview), and HTML artifacts all render through native Vue components (`HtmlArtifactViewer.vue`, `CsvViewer.vue`, plus text/iframe previews). Diagrams live inside HTML artifacts as inline SVG; `.excalidraw` is no longer a viewer type. History and Diff operate on the raw file snapshots.

Chat-originated file opens resolve a canonical absolute path through
`/api/chats/{chat_id}/file-path` before rendering or pinning. Relative paths are
anchored to that chat's agent root, never the sidebar's selected workspace.
Pins report success explicitly; failed pins keep the modal open, and an older
pin request cannot close a newer file preview.

The chat panel and pinned-file panel load when their views need them; a direct
chat or subagent link starts the chat download alongside bootstrap data. The
file viewer loads on first open and stays mounted afterward so dialog focus
restoration and edit state keep their normal lifecycle. The item peek host
(`ItemPeekHost.vue`, which mounts `TaskWorkspaceSurface.vue` in its overlay
mode) loads the same way, on the first task link opened over a chat; Back
closes it through `useOverlayBackEntry`. Home does
not fetch
these optional modules. The engine status request is bounded and shared between
startup progress and outage monitoring; a ready response within the brief
startup display delay avoids showing the curtain on a warm load.

During a live chat turn, Activity stays compact by default: one `Working` row
shows the latest safe activity description and elapsed time. Activate the row
with click, Enter, or Space to inspect the complete scrolling timeline; the
expanded choice is preserved at turn completion. New activity slides within
the preview, except when reduced motion is requested.

Failure recovery sends a server-guarded “continue”, not the request or its
attachments. Controls wait for matching fresh chat metadata and indexed history;
an unavailable or stale snapshot disables continuation actions but still allows
the observed schedule to be stopped. A conflicting saved retry is
shown separately and is never automatically stopped or replaced. An uncertain
outcome is retained across reloads, requires a disk-backed check of each exact
server-issued receipt ID and explicit reconciliation, and is never automatically
resubmitted. The receipt is saved locally before dispatch; a status check cancels
an issued but unsubmitted request before its marker can be cleared.
Creating a literal schedule from the sidebar requires the server to atomically
verify an empty slot; a conflict refreshes the displayed schedule. Running or
stopping a literal schedule requires confirmation and its observed server
identity; a conflict refreshes the schedule without acting again. If no identity
is available, these controls are disabled and it can still run on schedule.
Recovery rejects model and thinking changes until the server can atomically
guard them against a newer failure, and rejects provider handovers.

A running background subagent stays visible while its chat is idle. The engine's
`chat_subagents_ready` count and the `/api/subagents/running` list are separate
sources and the store never writes one into the other. When the list names a
running child of the active chat while the count is 0, the store refetches that
chat's full subagent list once and keeps both polls going, within the existing
90-second drain, until a successful list no longer names it. A failed request
keeps the rows, and a dedicated single-agent view still suppresses the full-list
poll.

Production hashed JavaScript and CSS under `/assets` use negotiated gzip above
500 bytes. Other static files, API responses, and chat streams keep their
existing response path. Conditional and ranged asset requests retain their
identity representation.

The chat rail uses Option/Alt+Left and Right to move through the current workspace's chats, including from the composer. Matching chords are consumed at the ends; the title's unmodified arrow-key navigation remains available. Configured Option shortcuts work in editable fields, including keys reported as dead keys; unbound dead keys, IME composition, AltGr, unmodified workspace digits and controls that claim their keys retain precedence. Explicit custom and disabled bindings are preserved.

## Dev workflow

The embedded LoginView keeps terminal guidance and the six-item product tour for ordinary CLI bootstrap. Native installer bootstrap uses the same setup form and provider diagnostics without the terminal shell, first-launch note, or tour; installer workspace suggestions remain authoritative until the user types or selects a folder.

```bash
cd web
npm install          # first-time only
npm run dev          # local Vite dev server (proxies API to localhost)
npm run build        # type-check + production build into ../ciao/web/static/
npm run build:harness  # the browser suite's terminal harness, into its own tree
npm test             # vitest
npm run test:e2e     # Playwright browser suite (see Testing below)
```

Type-checking runs via `vue-tsc --noEmit` as part of `npm run build`. PR-blocking minimum: `npm run build` passes.

The component workshop runs without an engine: `npm run storybook` serves it at
`http://127.0.0.1:6006`, and `npm run build-storybook` writes the ignored
`web/dist/storybook` output. It imports production components and CSS directly,
starting with TabBar; it does not load the app bootstrap or public assets.
`npm test` remains the separate application suite. Open **Inventory / UI
inventory** (`inventory--all`) to search the production owner catalogue. Each
SFC has its own row, including connected compositions, alongside inline and
shared CSS families. Search intersects with family, coverage and migration
filters. Direct consumers are production import/include sites; support sources
may use classes or host a composition without directly importing the owner.

An inventory row is not a story or a migration approval. **Covered** means a
rendered-state story exists, not that interaction is automatically verified;
**uncovered** means an implemented state has no story; **unknown** means the
state or loading fidelity has not been audited; **not applicable** means it is
known not to apply. Summary categories overlap. Loading notes distinguish the
actual owner and initial load from failure or stale refresh where known.
Migration starts unassessed unless an actual decision supports another status.
Today only the four TabBar states have rendered stories; chat, task and other
owners remain unassessed or uncovered. Adding an SFC requires a separate
inventory row (and a rationale for a composition), not a complete story suite.

After `npm run build-storybook`, run this from the repository root to check
the built index, including the type of each linked story:

```bash
node --experimental-strip-types --input-type=module -e 'import {readFileSync} from "node:fs"; import assert from "node:assert/strict"; import {inventory} from "./web/src/stories/inventory.ts"; const {entries}=JSON.parse(readFileSync("web/dist/storybook/index.json","utf8")); assert.equal(entries["inventory--all"]?.type,"story"); assert.equal(entries["inventory--all"]?.title,"Inventory/UI inventory"); for(const row of inventory) for(const state of row.states) for(const id of state.storyIds) { assert.equal(entries[id]?.type,"story",`${row.id}/${state.name}: ${id}`); assert.notEqual(id,"inventory--all"); } for(const suffix of ["default","counts","long-labels","narrow-pane"]) assert.equal(entries[`foundation-tab-bar--${suffix}`]?.type,"story");'
```

The Node tests check data and filters, not Vue interactions or manager links.
Check filtering, keyboard access, narrow/zoom layouts and story links in both
the development and built workshop in a browser; record screenshots and network
evidence with the change. Automated Storybook browser interactions are future work.

After changes that touch running static assets (anything that lands in `ciao/web/static/`), redeploy via the PWA's Settings → Deploy. **Do not restart the ciao service yourself**: you'd sever the PWA session the agent is talking through.

## Layout

Onboarding's Access & memory stage treats filesystem scope and runtime approval mode as separate choices. It writes only explicit changes through workspace settings; an unset permission mode remains unknown/engine default and is never presented as a known guarantee.

```
web/
  index.html              entry HTML, viewport meta, PWA manifest link
  src/
    main.ts               Vue bootstrap + iOS viewport / keyboard / zoom plumbing
    App.vue               root component; loads the global stylesheet
    styles/app.css        global CSS tokens (--bg, --fg, --accent; `data-text-contrast="soft"` on `<html>` lowers --fg; `data-body-font` swaps --font), wordmark + caret, shared focus utilities
    router.ts             routes: /login, /, /chat/:id, /project/:id, /schedules, /tasks,
                           /memory, /stats, /settings, /settings/:tab
    components/           one Vue SFC per feature pane (including HomeIntake, HomeReviewSummary, UsageView (the /stats Usage page; data from lib/usageStats.ts),
                           CommandPaletteModal, FileViewerModal, NotificationBell (the inbox bell and its popover or phone sheet), NotificationInboxList (the inbox body), HomeNoticeWindow, HousekeepingStrip (Home's single "Needs you" list above the composer: workspace-health errors, operator notices and update tasks, one title, one line and one primary button per row) with NeedsYouRow (the row and its ⋯ menu), and WebhookTriggers)
    components/TaskBoardView.vue  the `/tasks` route entry point: a thin wrapper that
                           renders `TaskWorkspaceSurface.vue` and forwards its
                           `open-sidebar` request and the open task's pull-to-close, and
                           exposes its previous / next task, close and New task to
                           ChatLayout. The surface owns the board, the full task editor,
                           resolution and delegation state. On `/tasks/<id>` the open task
                           is a page (the board kept mounted behind it, a TitleRail over
                           the board's tasks within its filter); a switch writes the open
                           task first under the overlay's strict close rule. Its
                           `mode="overlay"` + `taskId` form is the same editor over another
                           screen, with no board chrome and no address change, an awaited
                           `requestClose()`, and a `detail-close` that distinguishes the
                           editor's internal close before the delegation preview from final
                           closure. `ItemPeekHost.vue` mounts it in that mode for a task link
                           opened over a chat or a note, and Back closes it through
                           `useOverlayBackEntry`; the `/tasks` route keeps its own
                           editor and address.
    components/*.css      stylesheets two panes share, each pulled in with
                           `<style scoped src>` (overviewSections.css, chatTrace.css,
                           titleShimmer.css)
    components/settings/  panels split out of SettingsView.vue (the host's start-at-sign-in row is
                           SettingsEngineLogin.vue; SettingsMemoryHandover.vue is the Memory handover card:
                           agreement rate and the operator-only per-workspace handover choice;
                           SettingsWorkspaceOverview.vue is the Workspace > Overview page;
                           SettingsWorkspaceModels.vue is its Chats and models card;
                           SettingsClaudeTrim.vue is its Claude context trimming card (catalog from the engine, pure helpers in lib/claudeTrim.ts; edits the same draft, saved by Chats and models' Save changes);
                           WorkspaceGoogleAccount.vue is its Google account row and
                           GoogleConnectDialog.vue the Connect dialog;
                           SettingsAllWorkspaces.vue is Overview's last card: boxed
                           rows, the short New workspace form and Archived; rows reorder by
                           drag, Alt/Option+Up/Down on the handle, or Move up / Move down in
                           the row menu, through `store.reorderWorkspaces`, with the polite
                           announcement text from `lib/workspaceOrder.ts` (these rows keep their own native drag handle and do not use `useDragSort`); WorkspaceColorPicker.vue is the accent
                           swatch group both use: five base presets plus a dashed "+" disclosure for
                           blue, teal, lime and fuchsia), plus the
                           scoped CSS they share with it
    components/settings/SettingsVaultCheck.vue  the Check vault answers switch card (engine-wide, stored as verify_vault_answers)
    components/TurnVerificationBadge.vue  the always-visible Vault n/m toggle under a turn's footer, with its claim list (ChatPanel renders it on the closing bubble; the pure helpers are lib/turnVerification.ts)
    components/TitleRail.vue  the swipeable title rail of an open chat's, automation's or project's header (ChatPanel's `plainHeader` prop turns it off);
                           railMotion.css is the lean/arrival motion of the body under it
    components/SidebarRow.vue  the sidebar's one list row: chat rows, automation rows and the Settings and
                           Memory section rows; its `placeholder` mode is the row's loading shape, which
                           SidebarListSkeleton.vue lays out in groups for the sidebar's first loads
    components/ItemTitleRow.vue  an open item's header title row (close, then the rail or the title), shared by
                           ChatPanel, SchedulePanel, ProjectView and TaskWorkspaceSurface; ItemHeaderMenu.vue is the
                           shared ⋯ menu and headerFold.css the fold-wide / fold-phone classes that fold header
                           actions into it on a phone, with no media-query listener
    components/ListRow.vue  the page lists' one hairline row: Home's chats and the Automations overview;
                           lib/listRoaming.ts is the arrow-key roaming they (and the sidebar's automation list) share
    components/WorkspaceTile.vue  the tinted 1-9 / initial tile for a workspace on the settings pages
    components/MemoryHowItWorks.vue  the static "How memory works" disclosure, mounted once in
                           MemoryMapView under the header (so every Memory section shows it). Its open
                           state is the only storage, `ciao.memory.howOpen` ('1'/'0', read and written in
                           try/catch). The tag table is static data (`TAG_ROWS`), labelled through
                           `kindLabel`, and is cross-checked against the nightly and memory-pass prompts
                           in `ciao/stock/skills/ciao-memory/references/`
    stores/               Pinia stores (auth, projects, tasks, taskBoard, webhooks, fileViewer, itemPeek, notificationInbox), and store
                           modules (chatAnnotations) — see the ownership boundary below
                            `onboarding` owns lifecycle progress reads and revision-checked writes; onboarding
                            stages report their state to the presentation shell and never mutate progress directly.
                           `taskBoard` holds the server's card order (`order`, one id sequence per status); the store
                           never sorts or edits it locally, and its reorder and placement writes are serialised and
                           roll back on failure. `projects` owns `reorderProjects` and `projectOrderEpoch`, which
                           bumps when the project order changes outside the store's own reorder so an open sidebar
                           drag can cancel.
    composables/          reactive logic shared between components, and behaviour lifted
                          out of oversized panes (useHoverPinPopover, useChatComposer, useTitleRail (the generic rail model: items in sidebar order, an order frozen across rail switches, the arrival side),
                          useChatRail, useScheduleRail and useProjectRail (the chat, automation and project rails over it), usePullToClose (pull a title down to close),
                          useMcpServers, useModalFocus, useOverlayBackEntry (one history entry per open
                          overlay; Back closes it), useWorkspaceAdmin, useProviderChoices,
                          useGwsAccounts, useHomeNoticeWindows, useDragSort: the pointer drag-sort engine
                          for the task lanes and the sidebar's project list; there is no drag handle, so
                          `surfaceHandlers` lets a row's own non-interactive content drag on a fine pointer (a
                          `data-drag-primary` name control keeps its click below the threshold), while the row menu's
                          Move up / Move down call `move()` and the card's Move… dialog calls `place()` for the
                          keyboard and touch path (all four columns as destinations, a cross-lane
                          placement is one write, and a closed dialog or changed workspace cancels one); `settling` hands the
                          preview slot to the real row until the acknowledged order renders, so a drop does not
                          replay its neighbours in either lane; it persists nothing and calls `onCommit`;
                          useMenuFocusReturn keeps a menu action's trigger focus without stealing it back after
                          the user has focused or pressed elsewhere)
    lib/                  pure helpers (api, time, safeMarkdown, contrast, gwsProfileId, chatActions, permissionModes, notificationInbox, sourceHealth,
                          updateTasks, etc.) — no Vue imports; `taskOrder.ts` (the server order, lane derivation,
                          placement anchors and the preview mirror of the server's insertion) and `dragSort.ts`
                          (drop-slot geometry, settled order, auto-scroll step, announcement wording);
                          `titleRail.ts` (rail items, order, neighbours, settle index, labels, lean style), `scheduleList.ts` (the sidebar's automation groups, titles and badges)
```

## iOS PWA gotchas

Installed-app integration: the manifest uses the indigo shell colors, has
shortcuts for New chat, Memory and Automations, and advertises an optional
POST share target on browsers that support it. The service worker intercepts
`/share-target` and stages shared text, links and files (20 MB total) in
IndexedDB; it never posts them to the engine. Home asks the user to add them to
the draft or discard them. A share older than 24 hours is not opened, and
logout clears pending shares. The file viewer offers native Share where the
browser supports sharing that file type, alongside the existing Download.
Safari/iOS may not expose the installed app as a share target; ordinary file
attachment remains available there. PWA features need HTTPS or localhost.

Permanent install guidance lives in Settings → General → **Use Ciaobot as an app**
(`components/settings/SettingsAppInstall.vue`), not in the Home setup reminder:
that reminder can be closed for good, and its X is the only dismissal control
it has. The card keeps the general facts — installing is optional, the browser
already works, the host engine has to stay running, only a secure origin can
install or get push — and puts the per-platform steps (Safari on Mac, Chrome on
Mac/Windows, Edge on Windows, Safari on iPhone/iPad, Chrome on Android) behind
one disclosure, each row linking the vendor's own guide. It reports the reader's
current platform as a highlight, never a filter: all five rows are always on the
page, and it only claims what this window can see (`isStandalone`), because a
browser tab cannot know whether the app is installed on the device. Inside Ciaobot for Mac, which sets `window.__CIAOBOT_DESKTOP__` (`isDesktopClient()` in `lib/pwaPlatform.ts`), the card names the Mac app and drops the address row, the notifications card says the Mac app handles notifications, and the Home setup reminder is not shown.

The one-time *Your Ciaobot* card on Home (`components/HomeWelcomeCard.vue`,
state in `composables/useWelcome.ts`) shows the workspace folder, the generated
password and the AI tool while the engine's `.runtime/welcome-pending` marker
exists. The password is fetched only when the user presses Show or Copy, lives
only in the component's state and is cleared on dismiss and unmount. Only the
engine decides `on_host` (from `GET /api/welcome`); the PWA never infers it.
The X dismisses engine-wide through `/api/welcome/dismiss`, not per browser.

Phone setup lives permanently in Settings → Access & devices → Other devices
(`settings/SettingsDevices.vue`, anchor `#other-devices` on `/settings/access`); on the computer running
Ciaobot, the Home setup card keeps its three phone instructions and that link
inside a closed native disclosure. Setup task labels stay quiet, help links use
neutral bordered actions aligned with the row buttons, and narrow panes make
those actions full-width. Phone setup never keeps a completed reminder open.


The PWA runs primarily as a standalone iOS Safari app. Several iOS-specific quirks are addressed in code; do not undo them without reading why.

Chrome contract: the installed app uses an opaque black iOS status bar
(`apple-mobile-web-app-status-bar-style: black`) with white glyphs, in both
themes, and the viewport meta does not set `viewport-fit=cover`. iOS therefore
insets the viewport itself and the page never runs under the bar. Do not
reintroduce either: a translucent bar draws a material over the page, and
`cover` forces every surface to simulate the inset itself.

iOS 26 ignores both that meta and `theme-color` for the bar's colour: it fills
the bar solid only when an opaque fixed box (≥90% wide, >10px tall) touches the
top edge, and blurs the page otherwise. `App.vue`'s `.status-bar-tint` is that
box: a `--bg` strip as tall as the phone header's top padding, above every
overlay, in standalone on phones. Do not remove it or give it a translucent or
blurred background, or the bar goes back to blurring on every page and turning
the scrim's colour when the menu opens.

### Keyboard + viewport

- All of this lives in `lib/viewport.ts` (`installViewportPlumbing()`, called once from `main.ts`, idempotent) and is pinned by `lib/viewport.test.ts`. Add a test there for any change to the measurement rules. The module also exports `viewportHeight()` / `viewportWidth()` (the size a `position: fixed` element should measure against) and `onViewportChange()`, wrapped for components by `composables/useViewportHeight.ts`.
- Viewport meta in `index.html` carries `interactive-widget=resizes-content` on purpose. With that flag, when the iOS keyboard opens the layout viewport shrinks alongside the visual viewport. The keyboard-open detection relies on this: it tracks the tallest viewport height seen per orientation and toggles `html.keyboard-open` when the current `visualViewport.height` drops below ~85% of that max.
- `--app-h` is set to `window.visualViewport.height` (falling back to `innerHeight`) only while the keyboard is open, so the chat layout snaps instantly when it opens. When it closes the property is removed and the layout falls back to `100dvh`, the full screen in an installed PWA; a pixel height left behind there is the dead band under the composer. Plain `100dvh` alone is not enough while the keyboard is up on iOS Safari; it does not update until the user interacts with the page.
- `visualViewport` `scroll` events are intentionally NOT listened to. iOS fires them while the page shifts to keep the caret visible during multi-line typing, and re-reading `vv.height` there can latch a stale/smaller value, collapsing the messages area.
- The viewport is re-measured on `visibilitychange` (visible) and `pageshow`, each time as a short burst (0/50/200/500ms) rather than a single read. iOS suspends JS while the PWA is backgrounded, and if the keyboard was open it gets dismissed during the suspension with no `resize` ever reaching the page, so the keyboard-open state (and the `--app-h` pinning the layout to the shrunken viewport) stays latched and the layout keeps a dead zone under the input bar. The burst exists because `vv.height` still reports the stale value at the instant of resume. The resume path deliberately does NOT reset the tallest-seen height: if the keyboard is genuinely still up at resume, that would record the shrunken height as the maximum and keyboard-open would never be detected again.
- When the keyboard closes while a text field still holds focus (the user swiped it away), the field is blurred and the viewport re-measured. iOS otherwise keeps the page shifted for the focused field, leaving a status-bar-high gap above the pane header until focus moves.
- `--safe-top` stays as `env(safe-area-inset-top, 0px)` in CSS, a plain passthrough for platforms that report an inset. Never replace it with a JavaScript pixel measurement: a screen/viewport height difference cannot distinguish a top inset from space lost at the bottom. `lib/viewport.ts` owns viewport height and keyboard state, not safe-area measurement.
- `window.scrollY` is force-clamped to 0. iOS can still shift the document when the keyboard opens, leaving the input bar floating with a gap below it.
- There is no display-mode or keyboard override of the safe-area insets. The platform insets the viewport, so `--safe-bottom` is an `env()` passthrough like the others; a display-mode or `keyboard-open` rule would add a second inset wherever a platform reports one.

### Zoom and text scaling

Browser pinch zoom remains enabled for accessibility in both Safari tabs and
standalone PWA mode. Do not add `user-scalable=no`, `maximum-scale=1`, or
WebKit gesture-event blockers. Individual controls may use
`touch-action: manipulation` to avoid delayed/double activation without
disabling page zoom. The in-app font scale under Settings > Appearance is an
additional convenience, not a replacement for browser zoom.

iOS Safari auto-zooms the viewport when an input/textarea/select gets focus
if its computed `font-size` is below 16px. The global input rule in
`src/styles/app.css` already pins mobile inputs at `16px * var(--font-scale)`, and a
`@media (pointer: coarse)` carve-out re-pins them to a flat `16px` so
component-level overrides (markdown editor, comment compose) cannot drop
back below the zoom threshold. The chat composer is the one exception: it is
excluded from that flat pin and carries its own width-independent
`max(16px, var(--text-base))` floor under a coarse pointer, so its size
follows the transcript's body token (typed and sent text agree) without ever
falling under 16px. The desktop tightening override is gated on
`(pointer: fine)` rather than `(min-width: 769px)` so wide touch devices
(iPad portrait/landscape) never get the smaller typography. Do not weaken
either rule.

### HTML artifact highlights

Comment highlights inside an HTML artifact are painted with the CSS Custom Highlight API, which Safari only has from 17.2. Older iOS Safari and desktop-shell webviews still open artifacts, so the bridge (`ciao/web/artifact_bridge.py`) keeps a `<mark>` fallback for them. The fallback changes the document's DOM, and so it never wraps whitespace-only text or text that is a direct child of table structure. Keep the fallback until 17.2 is the floor.

### WebSocket suspension

iOS Safari suspends JS and WebSockets when the PWA is backgrounded. On resume, `readyState` may still report `OPEN` while no events flow. Listen for `visibilitychange` (visible) and `pageshow` (bfcache restore) on any view that depends on a WebSocket, and force-disconnect + reconnect.

### The browser terminal

The chat's and the project's *Open in Terminal* opens a terminal **inside the
app**, in the same right-hand tile a pinned file uses: the same inset
rounded window, the same draggable divider, the same remembered ratio. It
is a shell on the engine's own machine, and the pane says so before anything
is typed. The native host terminal keeps its own button
(`HostTerminalButton.vue`) for the actions that need a real window on the
engine's computer: provider sign-in, skill updates, the engine's own update.

- **The signed-in session is the terminal's trust boundary.** There is no
  password prompt: `stores/terminal.ts` asks the engine for the grant with
  an empty body when the browser holds none, and the engine issues it to a
  signed-in, same-origin browser. The grant still binds the browser, still
  expires after fifteen minutes, and is still revoked by a logout or a
  password change — it answers *which* browser may hold the socket and
  *for how long*, not *who knows the password*. The consequence is plain:
  a workspace that denies the agent `Bash` no longer has a host-shell
  boundary, because a signed-in browser is enough to reach a shell on the
  engine.

- **One entry per surface, outside the repository.** A chat's Chat details
  carries a *Terminal* row in its *Open* section — above *Repository* and
  *Context* — and a project page carries one under its *Repository* section. The
  terminal is not a repository feature: a chat with no linked folder still
  needs a shell (a provider sign-in, an install, an MCP server that stopped
  answering), so neither section lives inside the repository's, and a chat or
  a project without a repository — an auto-managed project included — has one
  too. The button is `components/BrowserTerminalButton.vue` and it is the only
  way in; opening the panel starts no shell.
- **One slot, two occupants.** The right-hand slot is the file panel's tile
  (`.chat-split-side`): the same inset, radius, border and shadow, behind the
  same `chat-split-resizer` divider, and the same remembered ratio
  (`ciao:chat-split-ratio`). A pinned file owns the slot. A terminal takes it
  when no file has and the pane is wide enough for both columns, and falls
  back to the full-screen drawer over the pane when it cannot — a phone, a
  pane too narrow, or a file that has the slot. The tile keeps the terminal
  mounted behind it, so the shell survives a hide; the drawer is the same
  overlay the phone has always had.
- **The shape comes from the room, not from the window.** The terminal's
  slot needs 360px for the chat column, 360px for the tile and its 24px of
  inset and grip, so a pane narrower than 744px cannot hold both and gets
  the drawer instead.
  `ChatLayout` measures the pane with a `ResizeObserver` and tells the pane
  which shape it is in; the pane draws the shape it is told. The phone rule
  still wins over both, so a phone-sized pane is always a drawer. The
  divider's drag clamps belong to the occupant: a file keeps the widths it
  has always had, and the terminal holds the chat column at 360px and the
  tile at 360px.
- `stores/terminal.ts` owns the wire — the HTTP actions, the socket, the
  bounded renderer backpressure and the replay offsets. It also owns the
  grant: it asks for one with an empty body when the browser holds none,
  and asks for a new one when the engine ends the stream because the grant
  expired or was revoked. The pane (`components/TerminalPane.vue`) owns the
  screen and the controls around it; the screen itself is
  `lib/terminalRenderer.ts` (one xterm, bound to one session for life).
- The pane is a chunk of its own, mounted the first time a terminal is asked
  for and kept after that, so the screen survives a hide and a chat page that
  never opens a terminal does not download it. `lib/terminalPanel.ts` carries
  the one-bit signal; Home's payload is guarded by
  `e2e/specs/chat-file-open.spec.ts`. The signal is a state, not an event:
  `ChatLayout` reads it when it mounts, so a page opened after a terminal was
  asked for still shows the pane.
- **The keyboard belongs to the shell while the terminal has focus.** Escape,
  Tab, the arrows, Ctrl+C and Ctrl+D reach the terminal, so the pane does not
  use `useModalFocus` — its capture-phase Escape would swallow the shell's
  own Escape — and it does not remap Ctrl+C or Ctrl+D. Instead the pane
  *isolates* the app's shortcuts: a keydown that bubbles out of the terminal
  is stopped at the pane root, in the **bubble** phase, so the terminal's own
  listener has already run. `Shift`+`Tab` is the one key intercepted in the
  capture phase: it moves the keyboard to the panel's controls, because the
  shell keeps Tab and a pane that cannot be left is a keyboard trap.
- **A soft keyboard gets a key row.** Under `(pointer: coarse)` the pane shows
  `components/TerminalKeyRow.vue` under the screen while a session has one:
  Esc, Tab, Ctrl+C, Ctrl+D, the four arrows and Enter, 44px native buttons
  disabled until the session is live. The bytes come from
  `lib/terminalKeys.ts` and go through `store.sendInput`, like a keystroke;
  the arrows follow the screen's cursor-key mode (DECCKM, read from
  `TerminalRenderer.applicationCursorKeys`), CSI normally and SS3 in
  application mode. A tap prevents the pointerdown and mousedown defaults so
  the focus (and the soft keyboard) stays on the shell, and a key that took
  the focus anyway hands it back to the screen.
- **Hide is not End.** Hide detaches: the socket closes and the shell keeps
  running on the engine. End asks the engine to stop it. They are two
  buttons, and only one of them stops the process.
- **The terminal belongs to the chat on screen.** It is keyed by
  `activePinKey`, like the pinned file. Leaving a chat hides it (a detach: the
  shell runs until the engine prunes it 15 minutes after it detached). Coming
  back reopens it only if it was open when the person left, and starts no
  shell. The state is per tab and in memory (`openChats` in
  `stores/terminal.ts`), and a switch moves no focus. A project's terminal
  hides on a chat change and is not restored.
- **A command card types the command, and never runs it.**
  `needs-you/CommandBody.vue` types through
  `useRunInTerminal(…).run(command, { chatId, paste: true })`, into a session
  the composable created for that chat (key `chat:<id>`), never into a shell
  the user opened. It wraps the text in `ESC[200~`/`ESC[201~` only when
  `store.bracketedPasteMode()` is true, sends a single line raw otherwise, and
  copies a multi-line command when the shell has no bracketed paste. It never
  appends `\r`, so the user presses Enter. The engine only hears
  `{step: 'typed'|'ran'}`.

### Side-by-side chats

A second chat can sit in the same right-hand tile the terminal and a pinned
file use. The tile has one occupant; the newest takes it.

- **Persistence:** `lib/chatSplit.ts` keeps one chat per workspace in
  `localStorage` under `ciao:beside-chat`. It is per browser on purpose: a
  phone cannot show the split, and the pinned file stays server-owned.
- **Precedence:** in `ChatLayout.vue`, `besideInTile` (a remembered chat, shown
  beside a chat route, on a pane at least 940px wide) gives the tile to the
  second chat; `fileInTile` is the pinned file unless that holds; the terminal
  takes the tile only when neither does.
- **Sockets:** `store.attachBesideChat` keeps the second chat live while it is
  on screen. `isViewedChat` treats both panes as viewed, so unread marks, read
  receipts and notifications are suppressed for both.
- **Panes:** `ChatPanel` takes `chatId`, `beside`, `inactive` and `split` props.
  The second pane's element ids carry a `beside-` prefix so the two never clash.
- **Keyboard:** `focusedPanel` routes the chat chords (question and permission
  digits, Enter, ⌘Enter, archive, the model picker) to the pane with the focus.
  Esc in the second pane closes only that pane.
- **File viewer:** `stores/fileViewer.ts` docks a file only into the chat that
  owns the tile. A file opened from the second pane opens the viewer.
  Folders dock the same way (`dockFolderInstead`), pinned as `<abs>/`, the folder-pin identity (D1). Re-docking the pinned folder bumps `folderRedock`, and the tile remounts its browser. `PinnedFilePanel` renders `FolderBrowser` for a folder pin and docks only `isPinnableFilePath` files from it (mirror of `_WORKSPACE_FILE_EXTS`; keep the two in step).

### Folder browser

`FolderBrowser.vue` is a read-only folder view on VueFinder 4.8.0, pinned exact.
It sits behind the file viewer, which loads it with `defineAsyncComponent`.

- **Chunk isolation:** `FolderBrowser.vue` is the only runtime importer of `vuefinder` and its CSS. `lib/folderBrowserDriver.ts` imports types only, so the store and node tests never load the package. VueFinder is the deliberate exception to "do not add a themed UI kit", kept to that chunk.
- **Config trap:** VueFinder 4.8 merges config as `{...defaults, ...passed, ...stored}`, so the `config` prop cannot set persisted keys (`view`, `showTreeView`, `theme`, …). `seedFolderBrowserConfig` writes `localStorage['vuefinder_config_ciao-folder-browser']` before mount. It keeps what the user toggled since, and forces `persist:false`, `fullScreen:false`, `path:''`. Non-persisted keys go through the prop.
- **First listing:** the component fetches the first folder itself (it needs the roots) and hands that listing to the driver as `initial`, so VueFinder's first `list()` does not fetch again. `onListing` reports each listing back for the "Showing the first N items." note, since `GET /api/workspace-dir` caps at 2000 entries.
- **Global stylesheet:** `vuefinder.css` is Tailwind v4 output. Its preflight is scoped to `.vuefinder`, but its layered utilities (`.hidden`, `.flex`, `.disabled`, …) and its `@layer base` `*` border-colour rule reach the whole app once the chunk loads. Unlayered app CSS wins over the layered utilities, so this only matters for an element that uses a utility class name with no rule of its own. The planning audit found one: `ChatPanel.vue`'s `.input-bar` `disabled` class, now removed. The `.fade-*` transition rule also shortens `App.vue`'s startup fade if it runs after the chunk loads. Accepted; re-audit on upgrade.
- **Teleports:** VueFinder teleports its breadcrumb dropdown and context menu to `<body>`. A Reka modal sets `pointer-events:none` on `<body>`, so the component's unscoped style restores `pointer-events:auto` on `body > .vuefinder__themer`.
- **Search is off.** VueFinder 4.8's search modal teleports to `<body>`, outside the Reka focus trap, so its input cannot be typed into.
- **Never pass `contextMenuItems: []`.** VueFinder's folder double-click navigates through the menu's `open` item, so the list is cut to `open` and `refresh`.
- **Touch:** VueFinder opens items on double-click only. Under `(pointer: coarse)` a single file selection opens it.
- **Font:** use `var(--font)`, never `var(--font-sans)`.
- **Folder links:** `isFolderLinkPath` (`lib/filePaths.ts`) makes a relative extensionless or trailing-slash link a `a.file-link` in chat and note markdown. It never claims a dotted last segment, a known extensionless name, a domain-shaped first segment or a dot-only path. Every click goes through `fileViewer.openLink`: a folder-shaped path asks `/api/workspace-dir` once and opens the folder on a 200; anything else takes the same `open()` as before.

### Layout traps

`PaneHeader.vue` owns the shared page-chrome rule: at viewport widths ≥769px,
`.chat-layout.sidebar-open` compacts page-tag headers that have no item-title
slot. Their h2 stays in the accessibility tree using visually hidden CSS,
never `display:none` or an `aria-hidden` ancestor. Actionless headers have no
layout footprint; an actions slot retains a compact trailing toolbar with the
page inset (Tasks' New task), including 44px coarse-pointer targets. Closing
the sidebar restores the full desktop header. Widths ≤768px keep the existing
mobile grid/lead/Back, with the bell still limited to ≤767px; item titles,
detail actions and automation forms are unchanged. Wrap the entire
ancestor/root/descendant selector in `:global(...)` inside scoped CSS: partial
global selectors can lose their descendants during Vue compilation.

- **Do not use `scrollIntoView` on nested scrollable containers.** iOS Safari can scroll the wrong ancestor. Compute `offsetTop` relative to the scroll container and call `scrollTo({ top, behavior: 'smooth' })` directly. See `scrollToHighlight` / `scrollSidebarToCard` in `ChatPanel.vue`.
- **Flex children with unbreakable content need `min-width: 0`.** Without it, a long unbreakable string (a URL, a model identifier, etc.) forces the flex parent wider than the viewport and breaks horizontal layout.
- **Tap targets** must hit the `--touch: 44px` minimum (declared in `src/styles/app.css`) on every coarse-pointer layout, not only below a viewport breakpoint. Icon-only buttons use `.btn-icon` or another expanded-hit utility. Visually small actions can wrap a 44px hit area around an 18px glyph instead of resizing the glyph. The browser suite measures all visible interactive controls on Home and Chat, not only controls carrying a utility class.
- **`position: fixed` popovers clamp through `lib/popoverAnchor.ts`**, against the *visual* viewport (`lib/viewport.ts`), not `window.innerHeight`. Anything a fixed popover pushes off screen is unreachable, because scrolling does not move it. A popover that focuses an input must also clamp *reactively*, via `useViewportHeight()`: the keyboard opens a moment after the box is placed, and a one-time measurement leaves it stranded behind the keyboard. `CommentComposePopover.vue` is the reference.

The app-wide task read keeps delegation and manual links separate: `taskForChat`
resolves only delegation, while `linkedTasksForChat` reads the task records'
`linked_chats` and exposes all of them in Chat details without attempt state or
completion actions. `isTaskChat` covers either relation for read-only signals;
archive, Needs you and closure logic continue to use delegation alone.

## Design system

CSS custom properties live in `src/styles/app.css` as `:root` declarations, loaded globally by `App.vue`. The system is opinionated:

- **Color**: Deep blue-violet surfaces (`--bg #1a1a2e`, `--bg2 #1f2240`, `--bg3 #2a2e54`, `--bg-elev #23264a`), orange accent (`--accent #fb923c`, `--accent-strong #ea580c`; per-workspace presets override it, and `neutral` grey stands in until the workspace's colour is known; labels on accent fills use `--on-accent #1a1a2e`), violet secondary (`--accent2 #6a47b8`). A clean light theme is supported via `.theme-light` overrides.
- **Type**: Body font (Settings > Appearance: System, Serif or Mono) sets `data-body-font` on `<html>`, which swaps `--font`; components use `var(--font)`, never `var(--font-sans)`. Hybrid system: proportional system sans for prose and controls; monospace (SF Mono, Fira Code, Cascadia Code) for code, commands, IDs, schedules, timestamps, and technical labels. Base scale: 11/12/14/16px (`--text-xs`, `--text-sm`, `--text-base`, `--text-lg`), dynamically adjusted via the client-side `--font-scale` multiplier (from 0.8x to 1.5x, configured under Settings > Appearance).
- **Geometry**: 10/6/14px radii (`--radius`, `--radius-sm`, `--radius-lg`). Spacing scale `--space-1` through `--space-6`.
- **Motion**: `--ease: cubic-bezier(0.2, 0.8, 0.2, 1)`.
- **Wordmark**: `BrandMark.vue` renders the supplied `/icons/icon-32.png` as a static 30×30px image with alt text “Ciaobot” in both themes, not a reload control; it has no cache or service-worker side effects. Only the expanded sidebar owns the mark. `PaneHeader.vue` has no mark or `brand` prop; it retains page tags, title/actions slots and mobile controls. A collapsed sidebar or closed phone drawer needs no logo: its toggle or hamburger still opens navigation. The terminal-style `.wordmark` (with size modifier `--lg|--md|--sm`) still renders `› word` in StartupView, UpdateProgressView, and LoginView.
- **Caret**: `.caret` is a blinking accent terminal caret. Pair it with the wordmark for "live" surfaces (login prompt, idle empty state).
- **Body and scroll:** the normal app surface is clean (no CRT grain); startup/update/native surfaces retain the terminal texture. Scrollbars are thin and visible so long files, tables, tabs, and settings panes never hide the fact that more content exists. The chat footer's turn-facts strip is the one exception: it scrolls sideways without a visible bar, because a bar under a reply reads as a rule across the transcript.

Shared utility classes (defined globally in `src/styles/app.css`): `.btn-primary`, `.btn-small`, `.btn-icon`, `.btn-chip`, `.badge` (with `--accent|--accent2|--muted|--success|--warn|--error|--dot`), `.page`, `.card`, `.form-grid`, `.form-group`, `.form-actions`, `.hint`, `.checkbox-pill`, `.modal-backdrop`, `.modal-sheet`, `.sr-only`. Modal-like surfaces use `composables/useModalFocus.ts` for initial focus, Tab containment, Escape capture, inert background content, and opener restoration; do not add a second window-level Enter shortcut that can confirm while Cancel is focused.

Prefer the utility classes over re-inventing the same button/badge/card per component.

## Conventions

- **Headless primitives migration.** `ConfirmDialog.vue`, `PromptDialog.vue`, `NewChatPicker.vue`, and `FileViewerModal.vue` use `reka-ui`'s unstyled Dialog primitives for modal semantics, focus trapping, Escape/outside dismissal, and focus restoration while keeping token-based markup, themes, and 44px controls. `CommentComposePopover.vue` keeps its app-specific Teleport, dismissal, and visual-viewport placement, but uses a Reka focus scope so it composes safely inside the file-viewer dialog. `ModelSelector.vue` uses a non-modal Reka Popover (with its searchable listbox kept inside the popover) and delegates fixed collision placement to Reka's Popper, including the triggerless mobile sheet. `ProjectSidebar.vue` uses Reka DropdownMenu roots, items, and submenus: the visible action buttons provide keyboard/touch entry points, while a right-click supplies a virtual pointer reference. The hover-preview read surfaces in `ChatCommentPopover.vue` and `PinnedFilePanel.vue` deliberately use a non-trapping, app-specific `FocusScope` plus measured positioning instead of Popover because hover must not steal focus and a click can pin the surface. The file viewer's internal read-comment popup uses a trapped nested `FocusScope` because it is a dialog-in-dialog with its own Escape and focus-return boundary. These bridges keep hover/pin state, local dismissal, focus return, and visual-viewport clamping; do not replace them with a focus-stealing modal primitive without changing that contract. Use `@vueuse/core` for small browser-state helpers when it removes custom lifecycle code; keep iOS visual-viewport measurement and clamping in `lib/viewport.ts` and `composables/useViewportHeight.ts`. Prefer Reka for new accessible overlays, retain hand-rolled code for app-specific layout or measurement, and do not add Tailwind or a themed UI kit. **A modal drawn inside a pane must put its `DialogOverlay` in a `DialogPortal`**: `.chat-main` declares `container-type: inline-size`, which makes it the containing block for `position: fixed`, so an in-place overlay dims and clips to the pane and leaves the sidebar live under a "modal" (`WebhookTriggers.vue` does this; `PromptDialog`/`ConfirmDialog` get it by living at the app root). Reka unmounts a closed dialog's content, so tests read a portalled dialog through `document`, never through `wrapper`.
- One Vue SFC per pane. Keep `<script setup lang="ts">`, template, scoped `<style>`.
- **Webhook triggers live in Automations**, not in a top-level inbox (#1034). `stores/webhooks.ts` owns one workspace's triggers through the same B4 load contract as `taskBoard` (`loadedWorkspace`, `loading`, `loadError`, `error`, a `requestSeq` read ticket and a `writeSeq` write ticket), and every write presents the `revision` the row was read at — a stale write is the server's 409 with the rows intact. **`mode`, `project_id` and `input_policy` are create-only**; `PATCH` accepts `name`, `instructions` and `enabled` and nothing else. **The store never holds a secret**: `create` and `rotate` return the one-time secret to their caller and no field keeps it, because `WebhookTriggers.vue` is the only surface allowed to show it, once. The receiver recipe (`lib/webhooks.ts`) carries the trigger's real id and a `<secret>` placeholder, never a credential — a recipe that outlived the copy button is not a place to keep one. The row, heading and section classes come from `components/overviewSections.css`, which both `SchedulePanel.vue` and the section pull in with `<style scoped src>`.
- Load states are separate states. A list that fetches (`ProposalReviewPanel`, `ProposalHistoryList`, the Memory Map) must distinguish first-load in flight, first-load failure (inline error + Retry, never an empty-state claim), a failed refresh over existing rows (keep the rows, mark them stale, offer Retry), a filter hiding a non-empty set (offer to clear filters), and a genuinely empty set. Never derive "there is nothing here" from a filtered array alone — a failed or pending GET would then read as a cleared queue. Load errors live in their own store slot (`loadError`), apart from action errors (`error`), so a list refresh cannot clear an unread accept/dismiss failure. `useTaskStore` exposes the same contract for schedules through `scheduleLoading`, `scheduleLoadError`, and `schedulesLoaded`; Home, Project, and Automations consume that shared truth rather than maintaining competing local interpretations. The workspace task board's store is `taskBoard`, deliberately not a second half of `tasks`: schedules are automations the engine runs, task records are the user's own, and every write in the board presents the `revision` it read so a stale edit is the server's 409 with the rows intact rather than a lost update. Its pure half — column bucketing, ordering, the filters, and the `/api/tasks*` error envelope — is in `lib/taskBoard.ts`.
- Store reads are computed state, not setup-store functions: Pinia exposes returned functions as actions, and invoking one activates that store's Pinia. A scroll or render path that only reads state must use a computed value (for example, per-chat pagination maps), while operations that mutate state or perform I/O remain actions.
- **A review row's available actions come from the payload, not from a re-derivation.** `VaultReviewPanel.vue` draws **Complete** in place of **Retire** when the candidate carries `completable: true`, and never draws both: a project that finished and a project that is wrong are different findings, and the user has to be able to tell them apart. The flag is computed in `ciao/vault_review.py` from the same helpers `complete_project_note` gates on, so the button that renders is a click the engine will honour. The PWA has no alias table and no view of the `projects/` layouts, so a client-side `evidence.type === 'project'` test is a second definition free to disagree with the first — and the disagreement is a refusal on a row that still looks actionable. The lede and the Discuss seed follow the same flag, so a project row is never asked whether it should be thrown away.
- The core work model is **request → run → output → durable knowledge**. `HomeIntake.vue` starts an ordinary project chat and preserves unsent text per workspace; `HomeReviewSummary.vue` uses active-workspace counts and labels checking/current/stale/empty/failed state explicitly and, on Home, is one docked window in the `.home-workbench` side rail beside the request column (stacking below it under a 940px pane), with `HomeTodayStats.vue`'s Today counts from `GET /api/stats` under its queues; `ChatPanel.vue` keeps the transcript dominant and opens a conditional, keyboard-operable Context / Activity / Output inspector. New UI should reinforce those relationships rather than introduce another top-level inbox or artifact silo.
- **A memory pass is not a chat row.** It is an app-owned chat in a hidden project, and it used to be listed twice for one archived conversation: once in Home's tiers under its own internal title (`Memory pass · …`), once as the conversation it works on. It is now one row, in the `memory insights` section `HomeRecentChats.vue` renders below the tiers, and the store filters a declared pass out of `activeChatsAll`, `projectChats`, `totalUnread`, the sidebar and every schedule target. Put the whole derivation — one row per archived conversation, the archive pipeline and the pass joined, every phase and its wording — in `lib/memoryInsights.ts`, not in the component; only the signals come from the store (`memoryInsightRows`). `ChatPanel.vue` names the source conversation above the transcript (and at the top of the Chat details rail, which shows it instead) and links the archived transcript, so an opened pass says what it is. `lib/memoryPass.ts` stays the only reader of the `memory_pass` helper kind, with the Memory project kind as the fallback for a helper that failed closed. The fallback is deliberately visible in `activeChatsAll` and `projectChats`: it has no memory-insight row, so its ordinary chat row is the only way back to its pending question. It is silent on every notification surface — toasts, the notification inbox, the bell and OS badge, the menu bar, `projectNeedsInput` and unread — even when blocked. The project kind is the app's own signal, so the engine keeps it to passes only: the Memory project holds the engine's own passes and nothing else, the create route refuses a client-supplied `memory_pass` helper, and the schedule and webhook pickers do not offer it as a target. A routine bound to it (or to a pass) is refused with a message that names the reason, and an existing one can still be paused or retargeted.
- Project, Memory, and file rows are native buttons or links whenever they perform work. Context menus use `role="menu"` / `role="menuitem"`, open focus on the first action, support Arrow/Home/End/Escape, and restore the trigger. Pointer-only rows and right-click-only actions are not acceptable.
- Markdown rendering goes through `lib/safeMarkdown.ts` (DOMPurify + marked + highlight.js). Never `v-html` raw user content. User chat bubbles use `renderUserMarkdown`, which escapes raw HTML (except the comment-context tags) so typed text shows as typed; assistant output uses `renderMarkdown`. A message that names a conversation the reader knows links to it under its human title (`lib/chatLinks.ts`): a bare id or the app's own `@chat/<id>` mention is linked, only for an id in the reader's own openable chat list, the raw id stays in the tooltip and in `data-chat-id`, and the label is built as text, so a title can never become markup. An image an assistant message embeds by workspace-relative path (`![shot](memory-vault/x.png)`) is served from `/api/workspace-image?path=…&chat_id=…`, which anchors the relative path to that chat's agent and vault roots (exact match, no fuzzy walk, so a message of made-up paths costs no directory walks; it anchors, it does not sandbox); left as written, the src resolved against the `/chat/...` route and loaded the app shell, a broken image. `ChatPanel` and `SubagentChatView` pass `{ chatId }` to `renderMarkdown` for it; web URLs and root paths (`/api/images/...`) stay as written.
- **After sanitization an anchor has a target or it is not an anchor.** The sanitizer drops a `file:`, `javascript:` or `data:` href, and the anchor used to survive it: still painted with the accent colour and an underline, still swallowing the click, pointing nowhere. `lib/safeMarkdown.ts` now renders such a link as inert text — a `<span class="markdown-dead-link">`, never an `<a>` — with the label and its inline formatting intact, and a file path kept visible as copyable text (a network path keeps its authority: `file://server/share` reads `//server/share`). The sanitizer's own URI policy decides (`DOMPurify.isValidAttribute`), so the two cannot drift apart, and the policy is not widened. One exception to the inert text: a local file path, in a surface that opted in and on the engine's own machine. A pane opts in by passing `{ openLocal: true }` to `renderMarkdown`/`renderUserMarkdown` (the chat bubbles in `ChatPanel`) or `renderVaultMarkdown(..., true)` (the file viewer and the pinned-file panel), and it may only ever opt in if its click chain acts on the button — the delegated `handleOpenLocalFileClick` handler. There the dead link becomes a real `<button class="markdown-dead-link--openable" data-local-path="...">` (the label and the visible, copyable path), and the handler asks the engine to open that path with the OS default application through `POST /api/workspace-open` (`lib/openWorkspaceFile.ts`). Every other markdown surface (`TaskNoteFeed`, `TaskWorkspaceSurface`, `SubagentChatView`, …) renders the dead local link as inert copyable text, because a clickable-looking control whose click goes nowhere is the very defect this bullet fixes. Off the engine's own machine — a phone or another computer on the LAN — the button is not rendered either, and the link stays inert text, because `/api/workspace-open` is loopback-gated on the TCP peer. `file://` navigation still never happens: there is no anchor left, and the engine request carries only the path. The neutralising pass runs last, after `linkifyHtml`, because that pass skips the content of an anchor and a dead link's label must not be turned into a workspace file link; it walks anchors only, so the openable button is left alone. This holds for `renderMarkdown`, `renderUserMarkdown` and `renderFileMarkdown` alike.
- **A link into the app navigates in place.** `lib/appLinks.ts` is the one allowlist: `/`, `/tasks`, `/schedules`, `/automations`, `/memory` and its review/map/categories/import/retired/history sections, `/settings`, `/settings/home` and the tabs in `SETTINGS_NAV`, the documented anchors (`#install-app`, `/settings/providers#chat-providers`, `/settings/workspace#new-workspace`), one task record by its full 32-hex id, and one conversation by its own id (`/chat/chat-0123abcd`, the shape the engine mints). It also accepts exactly one workspace File access target, `/settings/workspace?workspace=NAME#file-access`, where `NAME` is an engine-accepted workspace name (`^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`). `parseWorkspaceFileAccessHref` exports that parse for `SettingsView`, and the whole address is validated: one `workspace` parameter, the literal `#file-access` fragment, no extra or duplicate parameter, no other path, hash or origin. On arrival `SettingsView` expands that row, scrolls the settings pane itself (measured from its `scrollTop`, never `scrollIntoView`), focuses the File access select with `preventScroll`, and briefly tints the row. Reduced motion drops the tint and keeps the focus ring. This is navigation only: it never chooses or saves a value, issues no write, and never switches the active workspace. A missing workspace is named in a status that says nothing changed; a different row with unsaved edits stays open, its single name draft untouched, and the status asks the user to save or discard before the linked workspace is opened. A load failure stays the tab's error, not a missing-workspace claim. Renderers leave those anchors without `target="_blank"`, and the delegated handlers (`ChatPanel`, `PinnedFilePanel`, `FileViewerModal`) push the route in the same window, so Back and an unsent draft survive. A plain click on a task address (`/tasks/<id>`) from a chat, a pinned file, the file viewer or the delegated-chat note opens that task's full editor over the current view through `ItemPeekHost.vue` and `stores/itemPeek.ts`, with no URL change and one history entry at the same address (`useOverlayBackEntry`), so Back closes it through the same save; on `/tasks` routes, and for a task already open on the board, it pushes as before. The overlay is the board's own editor: a close, a route change or a workspace switch awaits `requestClose()`, so a blank title, a refused or incomplete save, or a busy nested sheet cancels the action and leaves the draft on screen. `parseTaskHref` and `isPlainLeftClick` in `lib/appLinks.ts` are the shared parse and modifier check. Modified clicks and the middle button stay the browser's (new tab/window), and a dirty dialog's refused close cancels the navigation. Every other href — an unknown path, an extra query, a different hash, `/api`, `/ws`, a protocol-relative or external URL — stays an ordinary link in a new tab. File links are separate: opening a file still goes through `data-file-path` to the viewer, not through a page address.
- **A proposal review link opens its exact question, or says why it cannot.** `/memory/review?show=suggested&workspace=<registered name>&proposal=prv_<64 hex>` (the `review_path` the listing and `ciao memory-proposals` return, parsed by `parseProposalReviewHref` so a canonical link in chat or note text also navigates in place) is resolved by `MemoryMapView.vue`, not by the route. It waits for workspace bootstrap, requires a registered workspace, switches scope through the store's guards with `switchWorkspace(name, { transition: false, isCurrent })`, loads the queue with a fresh `proposals.fetch()` (never `ensureLoaded`), then matches `review_key` within that workspace (the first pending equivalent row) and hands the row to `proposals.revealRow`, which clears the kind, decide and search filters. `isCurrent` is the scope-cancellation seam: it is checked before any switch and again after the guards answer, so a superseded link can never apply its workspace late. Every await re-checks a generation, so a route change, an unmount or a workspace change the resolver did not make withdraws the run and takes back only the reveal it handed off; `ProposalReviewPanel.revealRow` has its own generation, so a stale frame loop never focuses or clears a newer request. Outcomes are distinct: a malformed key or declined guard (refused), an unknown workspace (unavailable), a failed list request (**Could not load the proposal** with Retry, never a missing-state claim) and a key not in the pending queue (**This proposal is no longer pending.** with a `/memory/history` link). The link performs no decision and conveys no authentication: it needs a normal signed session.
- Chat Markdown tables use the renderer's `.markdown-table-scroll` region so compact tables shrink-wrap and wide tables scroll independently at narrow widths. Keep the region keyboard focusable and preserve readable key columns.
- DOM manipulation that needs to bypass Vue's scoped attribute (e.g. inline highlight spans inserted into rendered markdown) uses `:deep(...)` in the scoped stylesheet.
- **`ChatPanel.vue` ownership boundary.** The panel is being split in
  behaviour-preserving steps; put new work on the right side of the line.
  `composables/useChatComposer.ts` owns the composer — the draft and its
  synchronous persistence, prompt-history recall, textarea auto-sizing, caret
  insertion, and every attachment path (paste, drop, the native desktop drop
  grant, the file picker). It deliberately imports no store and registers no
  lifecycle hook: ids arrive as getters, the store arrives as the
  `ComposerAttachmentStore` interface, and `fetch` is injectable, so
  `composables/useChatComposer.test.ts` exercises all of it without mounting
  anything. Uploads go through `lib/chatAttachments.ts::postFormWithProgress` (XHR, for upload progress), injectable as `postForm`; `uploads` is the in-flight list `ComposerUploadProgress.vue` renders, and Send is disabled while it is non-empty. `ChatTurnActivity.vue` owns the rendering of one completed turn's
  `Activity` disclosure and owns no state — open/closed, the thinking
  preference and the markdown renderer are props, and every action is an emit.
  `ChatPanel` keeps the send path, the @-mention picker, the trace
  open/closed map, the memoised markdown cache, scroll anchoring and
  the live streaming trace. The slash-command list is shared with Home:
  `composables/useSlashCommands.ts` owns its state and text surgery,
  `SlashCommandPicker.vue` renders it, and `components/slashCommands.css`
  (scoped `src`, because the @-mention picker also uses those classes) styles
  it. Trace CSS lives in `components/chatTrace.css` and
  is pulled into both components with `<style scoped src>`; scoped rules in a
  parent do not reach a child's subtree, so moving markup into a component
  without moving its styles silently unstyles it.
- **Transcript render cost.** The dominant superlinear term in transcript
  rendering is gone, and `lib/renderScaling.test.ts` keeps it gone by counting
  work rather than timing it: `knownPathRulesBuildCount()` says how often the
  rules were actually computed, and rendering a 200-message transcript must
  compute them exactly once. A duration would depend on the machine and on
  what else the suite is running; this number does not. **A smaller
  superlinear term remains and is not guarded:** `knownPathMatches` still
  scans every rule for every text span, so cost still carries an
  O(paths x spans) factor and the path list still grows with the transcript.
  Removing it needs a real algorithm change — a trie, or one alternation
  regex — not another cache, so it was left out rather than half-done. The
  trap this section guards is `lib/filePaths.ts`: `renderMarkdown` linkifies known file paths, and
  the known-path list grows with the transcript. Rules derived from that list
  are memoised on the array's identity and built once per `linkifyHtml` call
  rather than once per text span — before #501 a 5x longer transcript cost ~85x
  the time. Two consequences for anyone touching that file: hand
  `buildKnownPathRules` a **new** array when the path set changes (mutating one
  in place is the shape the identity cache cannot see — `ChatPanel`'s
  `knownFilePaths` computed already does this), and keep rule-building out of
  any per-span or per-message loop.
- **`stores/projects.ts` ownership boundary.** The store is being split in
  behaviour-preserving steps; put new work on the right side of the line.
  `stores/chatAnnotations.ts` owns everything the user stages against the *next*
  message plus the notes and pins anchored to a file: the per-chat pending-image,
  pending-file-comment and pending-chat-comment buckets, the durable per-file
  comment store, and the seven `localStorage`
  keys behind them (`ciao-pending-images`, `ciao-pending-files`, `ciao-pending-comments`,
  `ciao-pending-chat-comments`, `ciao-file-comments`, `ciao-pinned-files`,
  `ciao-dismissed-auto-pins`). Staged files go out as a trailing `Attached files:` block (`composeAttachmentBlock`); user bubbles split it back into chips with `splitAttachmentBlock`, which reads both the `ciao-drop:` token form and the engine-expanded path form. **Chat pins are server-owned** (#1119): a chat's
  pinned file lives on the engine (`ChatInfo.pinned_file_path` /
  `dismissed_pin_paths` / `pin_revision`), is hydrated from chat payloads, the
  `chat_pin_changed` event and the `/ws/events` snapshot's `chat_pins`, and is
  mutated only through `PATCH /api/chats/{id}` `{pin:{path,expected_revision}}`.
  The browser never reads or writes a chat id from the two pin keys — those hold
  only legacy *project* pins, which the app no longer shows, and are pruned of
  any chat id a pre-#1119 build left behind. Manual chat pin/unpin awaits the acknowledged
  server state (a 409 adopts the server's truth and surfaces an error, no retry),
  and a per-chat pending flag disables the control while the write is in flight.
  It also composes an outgoing message from that
  material (`prepareMessage`) and clears it once sent
  (`consumePreparedAttachments`) — the send itself stays in the store. It is a
  plain `create*` factory, **not** a second Pinia store: `useProjectStore` calls
  it once in its setup and spreads the result, so the refs it hands over are the
  same refs it mutates and `store.pendingComments` behaves exactly as before. A
  second `defineStore` would have put a proxy and a second `$state` in between.
  `stores/chatAnnotations.test.ts` drives it with a bare `ref` for the active
  chat — no Pinia, no mount.
  **Needs-you actions are engine-owned and transient** (`ChatInfo.actions`, `chat_actions_changed`, the snapshot's `chat_actions`, kept in `stores/projects.ts`'s `chatActions` under the same read-start watermark and clock as the chat reads; `startChatAction`/`dismissChatAction`/`continueChatAction`/`submitChatActionCallback`; a 409 adopts the engine's list). ChatPanel renders them through `NeedsYouStack.vue`, which caps the region's height and folds two or more untouched sign-ins under one row (Show, Not now for all) in a single keyed list, so a card is never rebuilt when it leaves the fold. When a card appears or changes state the stack scrolls itself (never the page, never the focus) to that card's `data-needs-you-primary` element, else its `.needs-you-btn--primary`; `CallbackPasteField` and the device code carry the marker. `NeedsYouCard.vue` owns Not now and Continue, a per-kind body in `components/needs-you/` owns the primary control, and `CallbackPasteField.vue` is the shared paste-back field for a sign-in that ends on an unreachable loopback page, and `lib/chatActions.ts` owns the copy (`actionCopy` returns null for a kind with no UI yet, and such actions are not shown).

  Pure logic the store hands over whole lives in `lib/`, Vue-free and directly
  testable: `lib/chatHistory.ts` (history normalising, server-row mapping, turn
  grouping, metadata merge, superseded live-tail pruning, tool icons),
  `lib/chatQuestions.ts` (AskUserQuestion and capability-question parsing plus
  the picker signature), `lib/chatWs.ts` (per-chat reconnect policy) and
  `lib/safeList.ts`. The three names that used to be exported from
  `stores/projects.ts` itself — `shouldReconnectActiveChatOnStreamingStarted`,
  `chatWsReconnectDelayMs` and
  `setListIndex` — are re-exported from there, so importers do not move.

  Structured AskUserQuestion forms keep their header and answer actions fixed
  while a named, keyboard-scrollable details region owns long question content.
  Their 60dvh cap yields to the available flex height when a short pane and
  enlarged composer content need more room; if they cannot fit together, the
  chat column scrolls so the controls and composer remain independently reachable.
  The `narrow-viewport.spec.ts` browser tests measure footer visibility, hit
  targets, scroll-body room, font scaling and keyboard focus in real layout.
  The store keeps chats, projects and workspaces, message history and its
  reconciliation, every socket (per-chat and `/ws/events`) with its event
  handlers, unread and attention counts, the send path with its queue, deferred
  and unacked sends, the streaming timeline, toasts and package status.
- **The route contract: Home is unselected.** Reaching Home (`/` or a bare
  `/chat`) shows Home alone — no conversation, no pinned file — whatever was
  selected before it (#1116). `ChatLayout.vue` reconciles the store against the
  *path* (not only `params.chatId`, which is undefined on both sides of a
  secondary-page → `/` move), and calls `store.clearActiveChatView()` there.
  Secondary pages (Settings, Automations, Tasks, Memory, a project) still keep
  the selection underneath them, so a chat left for Automations is waiting when
  you come back; only the bare Home route clears it. Two rules follow:
  **Home navigation is a view change, not a deletion** — the chat, its
  composer draft, its staged attachments, its pin and any running turn survive,
  and the conversation stays in the list. `clearActiveChatView()` is therefore
  deliberately non-destructive (it detaches the selected chat's socket and clears
  the id; the global `/ws/events` awareness socket stays connected), while
  `closeChat()` keeps the destructive policy and remains the only path that
  deletes an empty draft. An explicit Close (or Escape on a chat route) is a
  request to discard that draft; clicking Home is not. **The pinned pane is
  scoped to the route on screen** (`activePinKey` in `ChatLayout.vue`): the chat
  id on a chat route, the project id on a project route, no key at all on Home,
  so Home can never render a retained chat's pinned page for even one frame, and
  reopening a conversation brings back *that* conversation's pin. **On a narrow
  device the same server-owned pin is a compact in-chat opener** (`.narrow-pin-opener`
  in `ChatLayout.vue`): a named, keyboard/touch-reachable button that opens the
  existing viewer, never a forced split pane or an auto-opened modal. It opens
  every kind the tile shows: an image pin goes through `openImage`
  (`fileViewer.openSharedPin`), since the text route refuses images with 415. A
  remote unpin removes the opener and, if its clean shared-pin preview is open,
  closes that preview — a separately opened viewer or a dirty edit is left
  untouched. Closing that preview with its × is an unpin (#1119), the same as the
  opener's own ×; Back only leaves it.
- **Workspace > Overview (`/settings/workspace`).** The first item of the
  workspace rail group is the page for `projectStore.activeWorkspace`:
  `SettingsWorkspaceOverview.vue` draws the Workspace, Claude context trimming,
  Chats and models and Google account cards (one editor, one Save changes /
  Discard row, in the last form card, Chats and models), then Memory handover, Update
  task history and, for an archivable workspace, Archive this workspace. The
  editor state is not in the component: `composables/useWorkspaceAdmin.ts` owns
  the registry forms, drafts, save, archive and restore, `SettingsView.vue` calls
  it once and `provide`s it (`WORKSPACE_ADMIN_KEY`), and the Overview `inject`s
  it, so the Overview cards and the All workspaces card share one load. Overview
  also renders `SettingsAllWorkspaces.vue`, the list of every workspace. A workspace switch
  drops the previous draft; one started from the UI while the draft is dirty
  asks first through `projectStore.setWorkspaceSwitchGuard`. A File access link
  (`/settings/workspace?workspace=NAME#file-access`) switches to NAME and lands
  on Overview with the selector focused; it never saves.
  `SettingsWorkspaceModels.vue` (Chats and models) edits the same form: the
  default provider switch and, for the selected provider, the workspace's
  `provider_overrides` (model, thinking, permission mode, session insights
  model). It loads the engine defaults itself (`/api/settings/routines`,
  `/api/models`) and builds its option lists with
  `composables/useProviderChoices.ts` (shared with the Providers page). The
  first option of every field is `Engine default (<value>)`, and an empty
  choice deletes the override. All edits go through
  `lib/workspaceOverrides.ts` (`normalizeOverrides`, `setOverride`) so
  `JSON.stringify(form)` stays a stable dirty check;
  `effectiveWorkspaceDefaultModel` feeds the schedule forms' "Inherit" label.
  The Google account card is one row for the active workspace
  (`WorkspaceGoogleAccount.vue`: connected, sign-in expired, or none) and the
  `GoogleConnectDialog.vue` modal sheet behind its Connect / Sign in again
  buttons. Both `inject` `useGwsAccounts` (`GWS_ACCOUNTS_KEY`), which holds every
  Google account call and sign-in flow; `SettingsView.vue` calls it once. The
  account id is derived from the workspace name (`lib/gwsProfileId.ts`, a mirror
  of `gws_auth.slugify_profile`; `tests/test_gws_auth.py` pins only its reserved-name list) and never
  typed. The workspace is linked when the account connects, through
  `useWorkspaceAdmin.setWorkspaceGwsLink`; saving the form cards never sends
  `gws_profile`. All workspaces has no Google card: an account no
  workspace links to has no screen.
- **`SettingsView.vue` ownership boundary.** Settings is being split the same
  way, one tab at a time, into `components/settings/`. General is split
  into four pages. General begins with the Updates section (compact to a title
  and an "Up to date" status when nothing is in progress), then the
  capability-help section linking to the public feature guide, then Use Ciaobot
  as an app, Notifications, Keyboard shortcuts (the binding list is collapsed
  by default), Resume after restart (`settings/SettingsRestartResume.vue`, the `resume_after_restart` routine setting), Start at sign-in and Open
  source. Appearance is
  the Appearance card alone: Theme and Font size rows in `SettingsView.vue`, and
  `settings/SettingsAppearance.vue` (Text contrast, Body font, Custom colours with `settings/CustomColorsDialog.vue`; `lib/contrast.ts` holds the WCAG maths and refuses unreadable pairs). Appearance is engine-wide:
  `composables/useAppearanceSettings.ts` mirrors `/api/settings/appearance`, applies
  it through `lib/appearance.ts`, and keeps `ciao-theme`, `ciao-font-scale` and
  `ciao-appearance` as paint caches only. Access & devices holds the PWA password, Other
  devices (`#other-devices` lives on `/settings/access`) and Network access.
  Advanced holds This host, Memory backup, the Ciaobot folder, Workspace health,
  the Developer switches and, in developer mode, Debug. `SettingsServer` takes a
  `section` prop (`network` or `developer`) so its two cards render on
  different pages.
  The Settings rail has two groups (`SETTINGS_NAV_GROUPS` in `lib/settingsNav.ts`):
  Ciaobot (General, Appearance, Access & devices, Providers, Advanced) and the
  active workspace, headed by its name and tinted initial tile. The
  workspace switcher menu ends with Workspace settings and New workspace. The workspace
  order is the server registry's list order: All workspaces (on Overview) reorders it with
  `store.reorderWorkspaces` (optimistic, rolled back on failure, `moveName` and
  `moveAnnouncement` in `lib/workspaceOrder.ts`; with no workspace named `personal` the main workspace is pinned first), and every list, the switcher and the
  1-9 shortcuts read `store.workspaceOptions` in that order. Providers is one card, so its page rail
  shows Status only, with no "On this page" list.
  The "On this page" navigation is built from the rendered sections in DOM order,
  so it follows that order. The MCP tab shows the provider inventory plus an
  "Ask Ciao" entry (a chat titled "Add an MCP server"), per-CLI tabs built on
  `TabBar` (the workspace's default provider first), a command builder (name
  and URL inputs, shell-quoted, with the engine's `cd` prefix from
  `/api/mcp/status` `shell`) and "Run in terminal", which types the command into
  the in-app terminal via `composables/useRunInTerminal.ts` and falls back to
  copying it when the terminal is locked or unavailable. The Providers tab's Connect does the same with the sign-in command the engine returns to a browser that is not on its computer (403 `not_on_host`), then the user presses Verify. `composables/useMcpServers.ts`
  owns status and usage reads and `setEnabled` (Turn off / Turn on, `POST /api/mcp/servers/enabled`, row patched in place), while `components/settings/SettingsMcpServers.vue`
  renders provider configuration and per-chat connection states.
  Settings edits native MCP configuration only to turn one server off or on
  (`POST /api/mcp/servers/enabled`); it never handles secret values or probes
  servers; commands run only in the terminal the user is watching. A server that
  needs a sign-in in an open chat gets **Sign in** instead of **Sign in in
  terminal** (which stays in the ··· menu): `store.requestMcpAuth` raises that
  chat's `mcp_auth` card (`POST /api/chats/{chat_id}/actions/mcp-auth`),
  `store.startChatAction` starts it as the card's own button does (for OpenCode
  this may start the chat's server), the sign-in page opens and the page goes to
  the chat. A terminal sign-in cannot update a running chat: OpenCode V2 has no
  reconnect route. The selected workspace scopes
  `/api/mcp/status`; only already running chats in that workspace contribute
  connection states. An empty connections list means unchecked, and unreadable
  configuration/provider state is unknown. Claude's workspace server allowlist
  remains authoritative and is never broadened by Settings. Add/authenticate
  servers with `claude mcp add` / `claude mcp login` or `opencode mcp add` /
  `opencode mcp auth` from that workspace's directory.
  Shared settings styling lives in `components/settings/settingsPanels.css`,
  loaded by both sides with `<style scoped src>` — a parent's scoped rules
  never reach a child, and the alternative is silently unstyled markup. New
  panels split out of Settings reuse that sheet rather than copying rules.
  Asset origin badges come from `lib/assetOrigin.ts`, which stays Vue-free.
- Completed chat traces stay collapsed as one compact `Activity` row. A settled active-session turn can hold an earlier explicitly completed reply before a hidden continuation; the server tags that row `phase: 'final_answer'` and the reader renders it as its own bubble, with compact `Activity` segments between it and the closing reply. The closing answer alone keeps the turn footer and `Outputs`. Touched-file chips sit below the final answer under `Outputs` (including files created via `Write` or common Bash redirects/`touch`/`cp`); interrupted turns keep their file chips inside `Activity` so unfinished work remains visible. Newly created files are labelled `new` on the chip.
- Conversation forks are initiated from the final assistant reply action group (Copy/Fork). The PWA sends the selected message slice up to that reply and redirects to the newly created chat, focusing the composer.
- New PWA actions (state-changing routes) must be documented in `../PWA_API.md` → Agent recipes, or whitelisted in `../tests/test_pwa_api_docs.py`.
- **The embedded terminal screen** (`lib/terminalRenderer.ts`) is a Vue-free primitive that owns one physical xterm bound to one session. The rules it keeps, and why: the session id is fixed at construction and there is no way to rebind it, because a screen that showed two sessions would be two histories at once; xterm's options object never leaves the module, so input is switched only by `setInputEnabled(boolean)` and starts off — a screen nobody has claimed sends nothing; a write is acknowledged only from xterm's own write callback, and the number it carries is the absolute end offset of the bytes just parsed, so a resume can use it and nothing else; a write that is still queued or in flight when the screen is disposed is **rejected, never resolved**, so a disposal never advances an acknowledged offset and the bytes come back on the next replay; the theme is read from the app's own tokens and resolved to the `rgb()`/`rgba()` values xterm can parse, and a `color-mix()` token is refused rather than passed on; one ResizeObserver, owned by the renderer, measures the screen and is disconnected on dispose. A link in the output (an OSC 8 hyperlink, or a plain `http(s)://` URL found by `@xterm/addon-web-links`) opens in a new tab with `noopener,noreferrer`, in the browser the user is on. Every other scheme is refused, and hovering a link names its host in the screen's tooltip (`Open <host>`). xterm and its fit and web-links addons are imported dynamically, so none of them enters the bundle of a page that shows no terminal — Home's payload is unchanged by the primitive existing. The pane that shows a screen is not in this repository yet.

- **The notification inbox is a derived view, not a source of truth.** The inbox, opened from the bell in `components/NotificationBell.vue` (body in `components/NotificationInboxList.vue`), projects four sources, keyed `chats`, `operator`, `automations` and `updateTasks`. `chats` is the `projects` chat predicates plus `taskSignals` board review, and carries board-result rows; a memory pass never contributes a row, blocked or not, because its lifecycle stays on Home's memory-insight rows; `operator` is `housekeeping.actions`, package and provider updates included, with `github-star` excluded; `automations` is `tasks.schedules` through `scheduleInWorkspace` and `scheduleNeedsAttention`; `updateTasks` is `housekeeping.updateTasks` through the shared `lib/updateTasks.ts` predicate that Home's strip also uses. `stores/notificationInbox.ts` only adapts those owners and tracks health, and `lib/notificationInbox.ts` is the pure projection. The count is distinct rows, not messages: a chat that needs the user and is unread is one *Needs you* row with an unread marker, a board result owns its review row, and an automation incident suppresses its linked failed chat. `99+` is visual only; the accessible name carries the full number. Reading is not resolving: opening the inbox marks nothing read, opening a chat clears unread through the existing `chat_read` lifecycle, and a pending question or permission stays a row. There is no dismiss, snooze or mark-all-read; resolve at the source. Exact links go to `/chat/<id>`, `/tasks/<id>`, `/schedules/<id>`, `/settings` or `/settings/providers`, and to `/?notice=<key>` for other operator notices and update tasks. A chat, task or automation whose target has gone re-reads, shows *That item is no longer available* and navigates nowhere. Freshness is per source (`checking`, `ready`, `stale`, `unavailable`); the worst one is the overall status, and an empty list is all clear only when every source is `ready`. Retry calls the owners' read methods only. `init()` runs from `ChatLayout` after bootstrap and `dispose()` on unmount; it installs one focus/visibility listener with a 5-second throttle and a scoped set of watchers, has no timer, and rejects late work with a generation token after a workspace switch. A new source gets a key in `INBOX_SOURCES` and a builder in the store's snapshot table, is seeded in every all-clear test fixture, and never gets a mutation path.

## Testing

- `npm test` runs vitest. This is where almost every test belongs.
- Mount smoke test: `src/components/__tests__/mountSmoke.test.ts` mounts every top-level pane to catch template / setup errors.
- Pure-function tests live next to the source (`lib/safeMarkdown.test.ts` pattern).

### Browser suite (`npm run test:e2e`)

`e2e/` holds a deliberately small Playwright suite that covers only the things a jsdom mount **cannot**
establish:

| Spec | What only a real browser can decide |
| --- | --- |
| `workspace-shortcuts.spec.ts` | Where a typed character actually lands. The `1`-`9` shortcuts must follow the visible sidebar order and stay inert while a text field is focused; jsdom reports a focused textarea that no keystroke is routed to. Also walks Tab through the primary nav, which is how a click-only control gets caught. |
| `chat-title-rail.spec.ts` | That the header's rail centres the title with the neighbours off-screen and the state track on the header's bottom edge, snaps and settles to a neighbour, slides and switches on the arrow key, keeps the 61px header and close on the title block's centre, and that the body never scrolls sideways.; and that dragging the title down closes the chat to Home, a short pull springs back, and a mouse drag sideways switches |
| `chat-motion.spec.ts` | That a chat opened from Home rises into place, archiving opens the next chat in the rail order rather than Home, and the phone drawer slides out on close with its rows still in it.; Chat details stays closed in the next chat once closed; and a pinned file slides in after the chat |
| `sidebar-overflow.spec.ts` | That the sidebar's chat list never scrolls sideways, with project rows idle or hovered. |
| `narrow-viewport.spec.ts` | Layout at 390px. jsdom has no layout engine: every rect is 0x0 and `scrollWidth` is always 0, so neither the unbreakable-flex-child trap nor a tap target under `--touch: 44px` is visible from a mount. The memory-insight journey also measures the row that carries a second control (the retry beside the open control), which is the one place a row can grow past the pane. Selecting a reply measures the opposite case: the action footer is not in the layout until the message is selected, and selection must leave the transcript's `scrollTop` alone; the fixture's opt-in `comment` shape then measures that the selected card's accent outline does not bleed across the turn gap onto the comment-reference card below it. Structured question cases measure the always-visible, hit-testable answer footer and usable scroll region at portrait, landscape and enlarged text, and check composer/keyboard focus. |
| `browser-zoom.spec.ts` | Reflow under page zoom and at the largest in-app font scale, and that the viewport meta never disables pinch zoom. |
| `task-board-lanes.spec.ts` | That every board lane is 310px wide at 1440, 1100, 900 and iPhone widths (computed flex included), that a review card's state word and action share one line, and that Done sits collapsed behind a 44px tab that expands, hides, persists across a reload, scrolls the Done lane into view and focuses its Hide button when the tab opens it, and accepts a dropped card (opening the completion sheet). The status filter still lists done tasks at full width. Widths and line boxes only exist in a real layout engine. |
| `task-card-alignment.spec.ts` | That every text block under a task card's title (meta line, agent note, archived caption, the band's state word) starts at the title's text column, and the band's dot centres under the Done ring, at 1280px and under iPhone touch emulation where the checkbox box is 44px instead of 28px. The hanging indent leans on both netting 20px, which only layout can confirm. |
| `webhook-actions.spec.ts` | Every trigger control and its delete menu stays within the viewport and passes corner hit-tests at desktop, 390px touch width and 200% page-zoom reflow, with History closed and open. Ancestor clipping can hide controls without causing document overflow. |
| `appearance-settings.spec.ts` | Settings › Appearance is engine-wide. Body font swaps the prose face while code stays monospace. Soft text contrast resolves `--fg` to `#d2d2de` / `#2e2f46` in dark and light, text contrast and theme survive a reload with the local caches cleared (only the engine can restore them), a refused custom pair keeps Apply disabled and says why while a passing one applies live and Reset clears it, and under Pixel 5 touch emulation every control in the card and in the Custom colours dialog is at least 44×44px with no sideways scroll at 390px. |
| `touch-targets.spec.ts` | Home, chat, tasks, automations, every Memory section, the project, schedule and task detail pages and every Settings tab under iPhone touch emulation: every control at least 44×44px. It also clicks each menu, popover and dialog trigger and measures what opens (model pickers, the workspace form, chat details), and checks `.field-info` panels stay inside the viewport and beside their glyph. Measures buttons, links, ARIA widgets, inputs (checkboxes and radios through their label), `summary` and opened `<details>`; inline prose links and visually hidden keyboard controls are exempt. jsdom applies no CSS, so only a real engine under `(pointer: coarse)` can measure it. |
| `events-reconnect.spec.ts` | That the *browser* notices a severed `/ws/events` socket, re-dials, and applies the snapshot the new socket carries. A vitest fake can only close itself. |
| `archived-chat.spec.ts` | That an archived chat opens read-only from a deep link: no composer, and no chat socket opened for a session the provider has already reclaimed. And that a project's archived list opens the transcript in the file viewer, with its "Continue this chat", instead of the chat's pinned side tile: the split view exists only in a real layout, so jsdom cannot see the tile the project route must not have. |
| `workspace-deep-links.spec.ts` | That a chat's workspace File access link reveals the right row, focuses its select inside the settings pane and issues no workspace write, at desktop, under direct load and on a phone with reduced motion. Row visibility, pane scrolling, the 44px touch target and 200% reflow are layout facts jsdom cannot see. |
| `workbench-layout.spec.ts` | That Home's review rail sits beside the command surface, that the expanded sidebar stacks workspace scope, New chat and the destinations without overlap, and that every boundary between Home's sections is the same gap token at desktop and at 390px (a per-section `margin-top` under a shared `gap` reads as a doubled gap, which no unit test can see). |
| `chat-loading-layout.spec.ts` | That the held history-loading skeleton has separated rows within the chat pane at desktop and phone widths. |
| `reply-not-folded.spec.ts` | That a short closing reply followed by a reasoning-only step renders as a bubble after a phase-less history replay (OpenCode shape) instead of being folded into the collapsed Activity trace (#630). The fold is a render heuristic, so only a real render shows it. |
| `note-verification.spec.ts` | That a review row which defers to a pending verification proposal lands on the proposal *in focus*. The link sets a row id and navigates, the queue's rows arrive with a fetch, and the panel is still behind the review filter's `v-show` for the first frames — a `nextTick` reveal focuses a `display: none` element, which is a silent no-op that no unit test can see. Also the copy, which is the only place a reader learns a dismissal declines rather than verifies, and the 44px touch minimum on a disclosure that reports a whole note's before/after. |
| `import-list.spec.ts` | That Memory → Import stays usable with a long listing at 390px. The controls a person has to reach — Review, Cancel, the confirmation — sit *below* the rows, and as a card on the map pane a 60-row listing pushed them thousands of pixels past the fold of a pane that does not scroll. `toBeInViewport` on a control at the bottom of that listing is the assertion jsdom cannot make. The two import routes are mocked in the spec rather than added to the fixture, so 60 synthetic sessions never enter shared fixture state. |
| `terminal-renderer.spec.ts` | That the terminal screen really draws. The bytes are written to a real xterm, a keystroke reaches the shell only while input is enabled, a resize changes the window, and a disposed screen leaves nothing behind — no DOM, no listener, no console output. jsdom has no xterm, no layout and no font metrics, so the screen itself can only be judged in a browser. The spec drives the real renderer through a test-only page (`e2e/fixture/harness/`), built into its own tree by `npm run build:harness` and served by the fixture under `/__fixture__/harness/`; the production build writes to a different directory, so the PWA that ships can never carry it. |
| `notification-inbox.spec.ts` | What a real browser decides for the inbox: the bell opens the inbox popover and Escape closes it without leaving the page; a second device's server-side `chat_read` converges over a real socket, and opening the inbox writes nothing; partial failure and stale rows are never all clear; a request still in flight across an A, B, A workspace round trip is rejected; an exact Home notice is revealed without running; a removed target shows *That item is no longer available* and stays; the collapsed-rail bell shows its count and partial ring; at 390px the pane-header bell opens the sheet and following a row closes it; and at 640px with a long hostile title nothing scrolls sideways, every link and button stays in the viewport, and reduced motion stops the inbox's animations. |

The fixture serves an empty chat history by default. A spec that needs real
turns to select opts in per session with `POST /__fixture__/transcript`, so the
specs beside it keep seeing the empty chat. `POST /__fixture__/verification` is
the same idea for the managed note-verification payloads: the review queue, the
proposal queue, the history ledger and the note graph all default to their empty
or plain states, and a spec that needs a pending proposal, a dead one, or a
settled verdict opts in. `POST /__fixture__/update-tasks` is the same opt-in for
the update-task rows of the "Needs you" list, which is empty by default because the Home gap
journey has to measure the sections around it. `POST /__fixture__/inbox` seeds or moves the chats, tasks, schedules, operator actions and socket frames the inbox reads, and `GET /__fixture__/writes` lists every non-GET `/api/` request the session has made. `POST /api/chats/<id>/read` now persists `last_read_at` and sends `chat_read` to every socket of the session, so two browser contexts sharing one `e2e_session` cookie behave as two devices.

Everything else stays in vitest. Adding to this suite is a trade, not a free
win: each spec is roughly a hundred times slower than the equivalent unit test
and can fail for reasons that have nothing to do with the code.

Ground rules, so the suite stays worth blocking CI on:

- **No sleeps.** Wait on an element or a polled condition. A `waitForTimeout` is
  both slow and the usual cause of a suite that is green locally and red on a
  loaded runner.
- **No retries.** `playwright.config.ts` sets `retries: 0` on CI as well. A test
  that needs a second attempt is broken; fix or delete it rather than masking it.
- **No one-shot reads after a release.** After `mouse.up()`, `dragTo()` or a
  synthetic `pointerup`, the app still has a re-render (and often an
  animation) to finish. Poll DOM state with `expect.poll`, and call
  `settled(locator)` from `e2e/support/app.ts` before reading geometry of
  anything that slides or resizes in. `src/lib/e2eSettleReads.test.ts`
  fails on `expect(await …)` or `boundingBox()` within a few lines of a
  release; a line that is genuinely stable can end in
  `// settle-ok: <why>`.
- **No real backend.** `e2e/fixture/server.mjs` is a dependency-free Node server
  that serves the built PWA out of `ciao/web/static`, answers the API routes with
  the invented workspace in `e2e/fixture/data.mjs`, and speaks just enough
  RFC 6455 to run a real WebSocket. It never reads a vault, holds a credential,
  spawns a model, or touches launchd, so a run cannot reach your own data.
  `/__fixture__/*` is its control surface (rewrite the snapshot, sever the
  socket); fixture state is keyed by an `e2e_session` cookie so specs running in
  parallel cannot see each other's.

```bash
npx playwright install chromium   # first time only, ~95 MB, outside node_modules
npm run test:e2e                  # prehook builds the PWA and terminal harness, then runs the suite
npm run test:e2e -- --headed      # watch it
npm run test:e2e -- --ui          # pick and step through a spec
```

`@playwright/test` is pinned exactly in `package.json`; the browser binary is
downloaded separately, so a contributor who never runs this suite pays only the
package (~18 MB of `node_modules`), not the browser.

CI runs it on Linux for develop PRs whose three-dot diff changes `web/`; the
job checks out the PR merge ref and runs `npm run test:e2e`, whose npm prehook
builds both the PWA and terminal harness from that checkout. The macOS `test`
job retains browser coverage on develop pushes and PRs into `main`.

### Still manual

Browser automation does not reproduce iOS PWA suspension, the software
keyboard, or native-shell behaviour. The device checklist in "iOS PWA gotchas"
above still has to be walked by hand on a real phone before a release.

The MCP Settings panel shows provider-native configuration separately from per-chat connection states returned by `/api/mcp/status`. No running observable session means connection not checked, and unreadable configuration/provider status is unknown.

Onboarding's care stage (`components/onboarding/OnboardingCareStage.vue`) asks when the host is usually available, then lets the operator edit the installed workspace-care routine's local time and IANA timezone. It updates only that existing row through the schedules API, and only for a time or zone the operator edited, when Continue commits it through the stage's exposed `save()`; unchanged fields write nothing. No routine is created when the workspace has none.

- The To decide filter chips are by change type, by reason, and *Needs you* (`decideFilter` in the proposals store, separate from `kindFilter` because History shares that one). *Needs you* keeps rows whose `decider` is not `nightly`, so rows with no decider count, and the chip shows only when at least one row is marked `nightly`. A row shows its `decider` and a one-line `decide_reason`, rendered as text.
- An optional reason is asked for before every individual accept and dismissal in the queue: the row's accept opens the decision card with the reason on it (`Why keep this? (optional)`), and every Dismiss opens an inline confirmation naming the proposal (`Why dismiss this? (optional)`). Picking the workspace for a re-home is the exception — the destination is the answer — and the batch bar never asks. There is no *Why?* disclosure left on a row or a card. The shared `DecisionReason` with the memory chips from `lib/memoryDecisionReasons.ts` holds the answer per row in the panel (`reasonById`), and it is discarded on Cancel, on a switch to another card, and after a successful decision; a failed dismissal keeps it so the retry does not lose the sentence. It is sent as `reason` only when non-empty, with that row's accept (and the retry of a deferred accept) or its dismissal, never on the batch bar; a refused accept clears it, the deferral keeping its own copy for the retry. With no reason typed the confirmation's button reads *Continue without a reason* and the card names the action above it (*This will save to ciao:memory.*, *This will dismiss the proposal.*); with a reason the accept button names the destination again. History renders a row's `reason` as text (`.ph-reason`), never markup.
- History shows a row's `prediction` under its text as predicted against decided, with a muted *Matched* or *Missed* badge (a miss is information, never an error) and the prediction `reason` as text. A `[memory]`/`[profile]` row the nightly run settled itself (`via: agent`) reads *Filed by Workspace care* (accepted) or *Dismissed by Workspace care* (dismissed), badges *Acted on prediction*, and offers *Undo* from the row when its change is undoable. The To decide queue never shows a prediction on a pending row, so the agreement rate stays unbiased.
- The facts inside a note (`VaultReviewPanel.vue`) are interpolated prose (`<p class="vr-entry-text">`), never `<pre><code>` and never `v-html`: the excerpt is capped and marker-stripped in `ciao/memory_audit.py`, and a note's own text stays literal. The section is a muted line of its own, so the fact's heading is not repeated in the grey context; `context` holds at most one same-section plain-prose line either side of the fact, and only lines `ciao/note_entries.py` classified as paragraph prose qualify: a thematic break, a table row, a quote, a fenced block, the frontmatter or a sibling bullet is skipped rather than quoted. A whole GFM table block is excluded by a display-only pass (`_table_line_flags`) after the walk, so sections and identities are untouched. Every heading the parser recorded is a hard boundary, including one separated by blank lines or several sibling bullets.
- Settings → Overview → *Memory handover* (`SettingsMemoryHandover.vue`, a three-way segmented control styled like Theme: arrows select) loads `GET /api/proposals/handover?workspace=` for the active workspace and saves only `{ memory_handover }` through `PATCH /api/workspaces/{name}`. New workspaces start at *Dismiss and accept*; existing workspaces without a stored choice remain *Off*. It never saves on its own: the suggestion is text. Choosing *Dismiss and accept* asks for confirmation, and the card says that it lets queued text reach always-loaded memory, with Undo in History and a cap of 5 changes per run. When the response's `acting_available` is false (a Linux/systemd install, where the gate trusts no service definition) the card says handover cannot act on this install and disables the options.

### Onboarding stages

`components/onboarding/` holds presentation stages that the shell composes; each receives
`OnboardingStageProps` (`context`, `busy`) and emits only `state`. The shell owns the one sticky
footer (Back, Leave setup, Continue; Finish setup on the last step) and is the scroll container,
because the page and body do not scroll. Stages are built from shared layout pieces that use the
DESIGN.md spacing tokens: `OnboardingStack` (md/xl/2xl rhythm), `OnboardingField` (label, control,
chips, help, error 8px apart, with the described-by ids), `OnboardingChoiceGroup` / `OnboardingChoice`
(labelled radiogroup of native radios, `list`, `cards` or `segmented`, 44px rows), `OnboardingStepFooter`
and `OnboardingButton`. Every choice is written when it is made, so there is no Save or Refresh step.
A stage reports readiness; the shell, never the stage, writes progress through the onboarding store.
Values the installer already set (folder, workspace name, runtime and model) are shown as set with a
Change control rather than asked again.

The onboarding location stage owns the folder-first location choice and workspace name validation; it reports readiness to the shared shell and never writes onboarding progress. Provisioned installs show the current folder and workspace as set; the folder links to the guarded workspace move flow and the workspace name is asked only after Change.
The onboarding finish stage lists only the summary rows that need attention (built from observed values), keeps the tools and video behind a Tips disclosure, and offers an optional
first task that creates a chat and sends the submitted text; it does not change setup progress
itself.

`OnboardingRuntimeStage` reads provider status and, for the selected signed-in runtime, the model list on mount; choosing a model writes it. It reads model provenance from
`/api/models`: runtime-reported models and configured aliases are rendered in separate sections,
and an unobserved runtime is never described as available.

## Onboarding memory

The Access & memory onboarding stage offers three plain-word handover options (Ask me first, Skip likely duplicates, File what's clear and ask about the rest) with no acknowledgement step; each is saved when chosen. The suggested last option applies only while that workspace's onboarding receipt is `in_progress`; it is not written until a choice is tapped or Continue commits it, and is saved only through the workspace's existing `memory_handover` setting. A missing trusted handover service is reported plainly and does not imply acceptance can act.

### Onboarding entry

`/onboarding` composes the stage components by the receipt's recorded step and advances progress only
through the onboarding store; `OnboardingView` never writes progress itself, and the access/memory
screen advances only when both components report ready. `HomeOnboardingResume` reads the current
receipt on Home entry and window focus and links into the journey while a target is pending — it is
not a dismissal, and it shows nothing when the receipt cannot be read.
