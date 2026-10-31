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
mode) loads the same way, on the first task link opened over a chat. Home does
not fetch
these optional modules. The engine status request is bounded and shared between
startup progress and outage monitoring; a ready response within the brief
startup display delay avoids showing the curtain on a warm load.

During a live chat turn, Activity stays compact by default: one `Working` row
shows the latest safe activity description and elapsed time. Activate the row
with click, Enter, or Space to inspect the complete scrolling timeline; the
expanded choice is preserved at turn completion. New activity slides within
the preview, except when reduced motion is requested.

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

## Dev workflow

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

After changes that touch running static assets (anything that lands in `ciao/web/static/`), redeploy via the PWA's Settings → Deploy. **Do not restart the ciao service yourself**: you'd sever the PWA session the agent is talking through.

## Layout

Onboarding's Access & memory stage treats filesystem scope and runtime approval mode as separate choices. It writes only explicit changes through workspace settings; an unset permission mode remains unknown/engine default and is never presented as a known guarantee.

```
web/
  index.html              entry HTML, viewport meta, PWA manifest link
  src/
    main.ts               Vue bootstrap + iOS viewport / keyboard / zoom plumbing
    App.vue               root component, global CSS tokens (--bg, --fg, --accent; `data-text-contrast="soft"` on `<html>` lowers --fg; `data-body-font` swaps --font), wordmark + caret, shared focus utilities
    router.ts             routes: /login, /, /chat/:id, /project/:id, /schedules, /tasks,
                           /memory, /settings, /settings/:tab
    components/           one Vue SFC per feature pane (including HomeIntake, HomeReviewSummary,
                           CommandPaletteModal, FileViewerModal, NotificationBell (the inbox bell and its popover or phone sheet), NotificationInboxList (the inbox body), HomeNoticeWindow, and WebhookTriggers)
    components/TaskBoardView.vue  the `/tasks` route entry point: a thin wrapper that
                           renders `TaskWorkspaceSurface.vue` and forwards its
                           `open-sidebar` request. The surface owns the board, the full
                           task editor, resolution and delegation state. Its
                           `mode="overlay"` + `taskId` form is the same editor over another
                           screen, with no board chrome and no address change, an awaited
                           `requestClose()`, and a `detail-close` that distinguishes the
                           editor's internal close before the delegation preview from final
                           closure. `ItemPeekHost.vue` mounts it in that mode for a task link
                           opened over a chat or a note; the `/tasks` route keeps its own
                           editor and address.
    components/*.css      stylesheets two panes share, each pulled in with
                           `<style scoped src>` (overviewSections.css, chatTrace.css,
                           titleShimmer.css)
    components/settings/  panels split out of SettingsView.vue (the host's start-at-sign-in row is
                           SettingsEngineLogin.vue; SettingsMemoryHandover.vue is the Memory handover card:
                           agreement rate and the operator-only per-workspace handover choice;
                           SettingsWorkspaceOverview.vue is the Workspace > Overview page;
                           SettingsWorkspaceModels.vue is its Chats and models card;
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
                          out of oversized panes (useHoverPinPopover, useChatComposer,
                          useMcpServers, useModalFocus, useWorkspaceAdmin, useProviderChoices,
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
    lib/                  pure helpers (api, time, safeMarkdown, contrast, gwsProfileId, notificationInbox, sourceHealth,
                          updateTasks, etc.) — no Vue imports; `taskOrder.ts` (the server order, lane derivation,
                          placement anchors and the preview mirror of the server's insertion) and `dragSort.ts`
                          (drop-slot geometry, settled order, auto-scroll step, announcement wording)
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
browser tab cannot know whether the app is installed on the device.

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

### Keyboard + viewport

- All of this lives in `lib/viewport.ts` (`installViewportPlumbing()`, called once from `main.ts`, idempotent) and is pinned by `lib/viewport.test.ts`. Add a test there for any change to the measurement rules. The module also exports `viewportHeight()` / `viewportWidth()` (the size a `position: fixed` element should measure against) and `onViewportChange()`, wrapped for components by `composables/useViewportHeight.ts`.
- Viewport meta in `index.html` carries `interactive-widget=resizes-content` on purpose. With that flag, when the iOS keyboard opens the layout viewport shrinks alongside the visual viewport. The keyboard-open detection relies on this: it tracks the tallest viewport height seen per orientation and toggles `html.keyboard-open` when the current `visualViewport.height` drops below ~85% of that max.
- `--app-h` is set off `window.visualViewport.height` (falling back to `innerHeight`), so the chat layout snaps instantly when the keyboard opens or closes. Plain `100dvh` is not enough on iOS Safari; it does not update until the user interacts with the page.
- `visualViewport` `scroll` events are intentionally NOT listened to. iOS fires them while the page shifts to keep the caret visible during multi-line typing, and re-reading `vv.height` there can latch a stale/smaller value, collapsing the messages area.
- The viewport is re-measured on `visibilitychange` (visible) and `pageshow`, each time as a short burst (0/50/200/500ms) rather than a single read. iOS suspends JS while the PWA is backgrounded, and if the keyboard was open it gets dismissed during the suspension with no `resize` ever reaching the page, so `--app-h` stays latched at the keyboard-open height and the layout keeps a dead zone under the input bar. The burst exists because `vv.height` still reports the stale value at the instant of resume. The resume path deliberately does NOT reset the tallest-seen height: if the keyboard is genuinely still up at resume, that would record the shrunken height as the maximum and keyboard-open would never be detected again.
- When the keyboard closes while a text field still holds focus (the user swiped it away), the field is blurred and the viewport re-measured. iOS otherwise keeps the page shifted for the focused field, leaving a status-bar-high gap above the pane header until focus moves.
- `--safe-top` stays as `env(safe-area-inset-top, 0px)` in CSS. Never replace it with a JavaScript pixel measurement: a cached startup value can stay at zero while the standalone viewport settles, hiding the header beneath the status bar. A screen/viewport height difference cannot distinguish a top inset from space lost at the bottom. `lib/viewport.ts` owns viewport height and keyboard state, not safe-area measurement.
- `window.scrollY` is force-clamped to 0. iOS can still shift the document when the keyboard opens, leaving the input bar floating with a gap below it.
- `html.keyboard-open` collapses `--safe-bottom` to 0 in `App.vue`. Without that, the home-indicator safe-area inset adds dead space below the input bar while the keyboard covers the home indicator.

### Zoom and text scaling

Browser pinch zoom remains enabled for accessibility in both Safari tabs and
standalone PWA mode. Do not add `user-scalable=no`, `maximum-scale=1`, or
WebKit gesture-event blockers. Individual controls may use
`touch-action: manipulation` to avoid delayed/double activation without
disabling page zoom. The in-app font scale under Settings > Appearance is an
additional convenience, not a replacement for browser zoom.

iOS Safari auto-zooms the viewport when an input/textarea/select gets focus
if its computed `font-size` is below 16px. The global input rule in
`App.vue` already pins mobile inputs at `16px * var(--font-scale)`, and a
`@media (pointer: coarse)` carve-out re-pins them to a flat `16px` so
component-level overrides (markdown editor, comment compose) cannot drop
back below the zoom threshold. The desktop tightening override is gated on
`(pointer: fine)` rather than `(min-width: 769px)` so wide touch devices
(iPad portrait/landscape) never get the smaller typography. Do not weaken
either rule.

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
  carries a *Terminal* section of its own — under *Repository*, above *Always
  loaded* — and a project page carries one under its *Repository* section. The
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
- **Hide is not End.** Hide detaches: the socket closes and the shell keeps
  running on the engine. End asks the engine to stop it. They are two
  buttons, and only one of them stops the process.
- **A suggested command is a card above the composer, not a second *Terminal*
  section.** When the assistant suggests a command in a chat, a transient card
  appears above the composer (`components/TerminalOfferCard.vue`): the fixed
  title *Suggested command — review before running*, the command as literal
  text (never markup, never markdown, never a link), and three actions —
  **Copy** (the exact text on screen, claiming *Copied* only when the
  clipboard took it, and a retryable error when it did not), **Open in
  Terminal** (the same `BrowserTerminalButton` the *Terminal* section uses, for
  that chat) and **Dismiss** (which names the suggestion it dismisses, so a
  newer one is never dismissed by mistake, and says so when the suggestion
  changed). The card never runs the command and never pastes it into the
  shell: *Open in Terminal* starts no shell and asks the engine to run
  nothing — an already-live session attaches and shows the screen it
  already holds, exactly like the *Terminal* section's button. The
  *Terminal* section stays the persistent, manual way into the terminal;
  the card is the contextual one, and it is shown only for the chat on
  screen.

### Layout traps

- **Do not use `scrollIntoView` on nested scrollable containers.** iOS Safari can scroll the wrong ancestor. Compute `offsetTop` relative to the scroll container and call `scrollTo({ top, behavior: 'smooth' })` directly. See `scrollToHighlight` / `scrollSidebarToCard` in `ChatPanel.vue`.
- **Flex children with unbreakable content need `min-width: 0`.** Without it, a long unbreakable string (a URL, a model identifier, etc.) forces the flex parent wider than the viewport and breaks horizontal layout.
- **Tap targets** must hit the `--touch: 44px` minimum (declared in `App.vue`) on every coarse-pointer layout, not only below a viewport breakpoint. Icon-only buttons use `.btn-icon` or another expanded-hit utility. Visually small actions can wrap a 44px hit area around an 18px glyph instead of resizing the glyph. The browser suite measures all visible interactive controls on Home and Chat, not only controls carrying a utility class.
- **`position: fixed` popovers clamp through `lib/popoverAnchor.ts`**, against the *visual* viewport (`lib/viewport.ts`), not `window.innerHeight`. Anything a fixed popover pushes off screen is unreachable, because scrolling does not move it. A popover that focuses an input must also clamp *reactively*, via `useViewportHeight()`: the keyboard opens a moment after the box is placed, and a one-time measurement leaves it stranded behind the keyboard. `CommentComposePopover.vue` is the reference.

The app-wide task read keeps delegation and manual links separate: `taskForChat`
resolves only delegation, while `linkedTasksForChat` reads the task records'
`linked_chats` and exposes all of them in Chat details without attempt state or
completion actions. `isTaskChat` covers either relation for read-only signals;
archive, Needs you and closure logic continue to use delegation alone.

## Design system

CSS custom properties live in `App.vue` as `:root` declarations. The system is opinionated:

- **Color**: Deep blue-violet surfaces (`--bg #1a1a2e`, `--bg2 #1f2240`, `--bg3 #2a2e54`, `--bg-elev #23264a`), orange accent (`--accent #fb923c`, `--accent-strong #ea580c`; per-workspace presets override it, and `neutral` grey stands in until the workspace's colour is known; labels on accent fills use `--on-accent #1a1a2e`), violet secondary (`--accent2 #6a47b8`). A clean light theme is supported via `.theme-light` overrides.
- **Type**: Body font (Settings > Appearance: System, Serif or Mono) sets `data-body-font` on `<html>`, which swaps `--font`; components use `var(--font)`, never `var(--font-sans)`. Hybrid system: proportional system sans for prose and controls; monospace (SF Mono, Fira Code, Cascadia Code) for code, commands, IDs, schedules, timestamps, and technical labels. Base scale: 11/12/14/16px (`--text-xs`, `--text-sm`, `--text-base`, `--text-lg`), dynamically adjusted via the client-side `--font-scale` multiplier (from 0.8x to 1.5x, configured under Settings > Appearance).
- **Geometry**: 10/6/14px radii (`--radius`, `--radius-sm`, `--radius-lg`). Spacing scale `--space-1` through `--space-6`.
- **Motion**: `--ease: cubic-bezier(0.2, 0.8, 0.2, 1)`.
- **Wordmark**: `.wordmark` (with size modifier `--lg|--md|--sm`) renders `› word` with an accent chevron prefix. Used in StartupView, UpdateProgressView, LoginView, ProjectSidebar brand, and empty states.
- **Caret**: `.caret` is a blinking accent terminal caret. Pair it with the wordmark for "live" surfaces (login prompt, idle empty state).
- **Body and scroll:** the normal app surface is clean (no CRT grain); startup/update/native surfaces retain the terminal texture. Scrollbars are thin and visible so long files, tables, tabs, and settings panes never hide the fact that more content exists. The chat footer's turn-facts strip is the one exception: it scrolls sideways without a visible bar, because a bar under a reply reads as a rule across the transcript.

Shared utility classes (defined globally in `App.vue`): `.btn-primary`, `.btn-small`, `.btn-icon`, `.btn-chip`, `.badge` (with `--accent|--accent2|--muted|--success|--warn|--error|--dot`), `.page`, `.card`, `.form-grid`, `.form-group`, `.form-actions`, `.hint`, `.checkbox-pill`, `.modal-backdrop`, `.modal-sheet`, `.sr-only`. Modal-like surfaces use `composables/useModalFocus.ts` for initial focus, Tab containment, Escape capture, inert background content, and opener restoration; do not add a second window-level Enter shortcut that can confirm while Cancel is focused.

Prefer the utility classes over re-inventing the same button/badge/card per component.

## Conventions

- **Headless primitives migration.** `ConfirmDialog.vue`, `PromptDialog.vue`, `NewChatPicker.vue`, and `FileViewerModal.vue` use `reka-ui`'s unstyled Dialog primitives for modal semantics, focus trapping, Escape/outside dismissal, and focus restoration while keeping token-based markup, themes, and 44px controls. `CommentComposePopover.vue` keeps its app-specific Teleport, dismissal, and visual-viewport placement, but uses a Reka focus scope so it composes safely inside the file-viewer dialog. `ModelSelector.vue` uses a non-modal Reka Popover (with its searchable listbox kept inside the popover) and delegates fixed collision placement to Reka's Popper, including the triggerless mobile sheet. `ProjectSidebar.vue` uses Reka DropdownMenu roots, items, and submenus: the visible action buttons provide keyboard/touch entry points, while a right-click supplies a virtual pointer reference. The hover-preview read surfaces in `ChatCommentPopover.vue` and `PinnedFilePanel.vue` deliberately use a non-trapping, app-specific `FocusScope` plus measured positioning instead of Popover because hover must not steal focus and a click can pin the surface. The file viewer's internal read-comment popup uses a trapped nested `FocusScope` because it is a dialog-in-dialog with its own Escape and focus-return boundary. These bridges keep hover/pin state, local dismissal, focus return, and visual-viewport clamping; do not replace them with a focus-stealing modal primitive without changing that contract. Use `@vueuse/core` for small browser-state helpers when it removes custom lifecycle code; keep iOS visual-viewport measurement and clamping in `lib/viewport.ts` and `composables/useViewportHeight.ts`. Prefer Reka for new accessible overlays, retain hand-rolled code for app-specific layout or measurement, and do not add Tailwind or a themed UI kit. **A modal drawn inside a pane must put its `DialogOverlay` in a `DialogPortal`**: `.chat-main` declares `container-type: inline-size`, which makes it the containing block for `position: fixed`, so an in-place overlay dims and clips to the pane and leaves the sidebar live under a "modal" (`WebhookTriggers.vue` does this; `PromptDialog`/`ConfirmDialog` get it by living at the app root). Reka unmounts a closed dialog's content, so tests read a portalled dialog through `document`, never through `wrapper`.
- One Vue SFC per pane. Keep `<script setup lang="ts">`, template, scoped `<style>`.
- **Webhook triggers live in Automations**, not in a top-level inbox (#1034). `stores/webhooks.ts` owns one workspace's triggers through the same B4 load contract as `taskBoard` (`loadedWorkspace`, `loading`, `loadError`, `error`, a `requestSeq` read ticket and a `writeSeq` write ticket), and every write presents the `revision` the row was read at — a stale write is the server's 409 with the rows intact. **`mode`, `project_id` and `input_policy` are create-only**; `PATCH` accepts `name`, `instructions` and `enabled` and nothing else. **The store never holds a secret**: `create` and `rotate` return the one-time secret to their caller and no field keeps it, because `WebhookTriggers.vue` is the only surface allowed to show it, once. The receiver recipe (`lib/webhooks.ts`) carries the trigger's real id and a `<secret>` placeholder, never a credential — a recipe that outlived the copy button is not a place to keep one. The row, heading and section classes come from `components/overviewSections.css`, which both `SchedulePanel.vue` and the section pull in with `<style scoped src>`.
- Load states are separate states. A list that fetches (`ProposalReviewPanel`, `ProposalHistoryList`, the Memory Map) must distinguish first-load in flight, first-load failure (inline error + Retry, never an empty-state claim), a failed refresh over existing rows (keep the rows, mark them stale, offer Retry), a filter hiding a non-empty set (offer to clear filters), and a genuinely empty set. Never derive "there is nothing here" from a filtered array alone — a failed or pending GET would then read as a cleared queue. Load errors live in their own store slot (`loadError`), apart from action errors (`error`), so a list refresh cannot clear an unread accept/dismiss failure. `useTaskStore` exposes the same contract for schedules through `scheduleLoading`, `scheduleLoadError`, and `schedulesLoaded`; Home, Project, and Automations consume that shared truth rather than maintaining competing local interpretations. The workspace task board's store is `taskBoard`, deliberately not a second half of `tasks`: schedules are automations the engine runs, task records are the user's own, and every write in the board presents the `revision` it read so a stale edit is the server's 409 with the rows intact rather than a lost update. Its pure half — column bucketing, ordering, the filters, and the `/api/tasks*` error envelope — is in `lib/taskBoard.ts`.
- Store reads are computed state, not setup-store functions: Pinia exposes returned functions as actions, and invoking one activates that store's Pinia. A scroll or render path that only reads state must use a computed value (for example, per-chat pagination maps), while operations that mutate state or perform I/O remain actions.
- **A review row's available actions come from the payload, not from a re-derivation.** `VaultReviewPanel.vue` draws **Complete** in place of **Retire** when the candidate carries `completable: true`, and never draws both: a project that finished and a project that is wrong are different findings, and the user has to be able to tell them apart. The flag is computed in `ciao/vault_review.py` from the same helpers `complete_project_note` gates on, so the button that renders is a click the engine will honour. The PWA has no alias table and no view of the `projects/` layouts, so a client-side `evidence.type === 'project'` test is a second definition free to disagree with the first — and the disagreement is a refusal on a row that still looks actionable. The lede and the Discuss seed follow the same flag, so a project row is never asked whether it should be thrown away.
- The core work model is **request → run → output → durable knowledge**. `HomeIntake.vue` starts an ordinary project chat and preserves unsent text per workspace; `HomeReviewSummary.vue` uses active-workspace counts and labels checking/current/stale/empty/failed state explicitly and, on the Today surface, sits in the `.home-workbench` side rail beside the request column (stacking below it at a 980px container width); `ChatPanel.vue` keeps the transcript dominant and opens a conditional, keyboard-operable Context / Activity / Output inspector. New UI should reinforce those relationships rather than introduce another top-level inbox or artifact silo.
- **A memory pass is not a chat row.** It is an app-owned chat in a hidden project, and it used to be listed twice for one archived conversation: once in Home's tiers under its own internal title (`Memory pass · …`), once as the conversation it works on. It is now one row, in the `memory insights` section `HomeRecentChats.vue` renders below the tiers, and the store filters it out of `activeChatsAll`, `projectChats`, `totalUnread`, the sidebar and every schedule target. Put the whole derivation — one row per archived conversation, the archive pipeline and the pass joined, every phase and its wording — in `lib/memoryInsights.ts`, not in the component; only the signals come from the store (`memoryInsightRows`). `ChatPanel.vue` names the source conversation above the transcript (and at the top of the Chat details rail, which shows it instead) and links the archived transcript, so an opened pass says what it is. `lib/memoryPass.ts` stays the only reader of the `memory_pass` helper kind.
- Project, Memory, and file rows are native buttons or links whenever they perform work. Context menus use `role="menu"` / `role="menuitem"`, open focus on the first action, support Arrow/Home/End/Escape, and restore the trigger. Pointer-only rows and right-click-only actions are not acceptable.
- Markdown rendering goes through `lib/safeMarkdown.ts` (DOMPurify + marked + highlight.js). Never `v-html` raw user content. User chat bubbles use `renderUserMarkdown`, which escapes raw HTML (except the comment-context tags) so typed text shows as typed; assistant output uses `renderMarkdown`. A message that names a conversation the reader knows links to it under its human title (`lib/chatLinks.ts`): a bare id or the app's own `@chat/<id>` mention is linked, only for an id in the reader's own openable chat list, the raw id stays in the tooltip and in `data-chat-id`, and the label is built as text, so a title can never become markup.
- **After sanitization an anchor has a target or it is not an anchor.** The sanitizer drops a `file:`, `javascript:` or `data:` href, and the anchor used to survive it: still painted with the accent colour and an underline, still swallowing the click, pointing nowhere. `lib/safeMarkdown.ts` now renders such a link as inert text — a `<span class="markdown-dead-link">`, never an `<a>` — with the label and its inline formatting intact, and a file path kept visible as copyable text (a network path keeps its authority: `file://server/share` reads `//server/share`). The sanitizer's own URI policy decides (`DOMPurify.isValidAttribute`), so the two cannot drift apart, and the policy is not widened. One exception to the inert text: a local file path, in a surface that opted in and on the engine's own machine. A pane opts in by passing `{ openLocal: true }` to `renderMarkdown`/`renderUserMarkdown` (the chat bubbles in `ChatPanel`) or `renderVaultMarkdown(..., true)` (the file viewer and the pinned-file panel), and it may only ever opt in if its click chain acts on the button — the delegated `handleOpenLocalFileClick` handler. There the dead link becomes a real `<button class="markdown-dead-link--openable" data-local-path="...">` (the label and the visible, copyable path), and the handler asks the engine to open that path with the OS default application through `POST /api/workspace-open` (`lib/openWorkspaceFile.ts`). Every other markdown surface (`TaskNoteFeed`, `TaskWorkspaceSurface`, `SubagentChatView`, `HousekeepingStrip`, …) renders the dead local link as inert copyable text, because a clickable-looking control whose click goes nowhere is the very defect this bullet fixes. Off the engine's own machine — a phone or another computer on the LAN — the button is not rendered either, and the link stays inert text, because `/api/workspace-open` is loopback-gated on the TCP peer. `file://` navigation still never happens: there is no anchor left, and the engine request carries only the path. The neutralising pass runs last, after `linkifyHtml`, because that pass skips the content of an anchor and a dead link's label must not be turned into a workspace file link; it walks anchors only, so the openable button is left alone. This holds for `renderMarkdown`, `renderUserMarkdown` and `renderFileMarkdown` alike.
- **A link into the app navigates in place.** `lib/appLinks.ts` is the one allowlist: `/`, `/tasks`, `/schedules`, `/automations`, `/memory` and its review/map/categories/import/retired/history sections, `/settings`, `/settings/home` and the tabs in `SETTINGS_NAV`, the documented anchors (`#install-app`, `/settings/providers#chat-providers`, `/settings/workspace#new-workspace`), one task record by its full 32-hex id, and one conversation by its own id (`/chat/chat-0123abcd`, the shape the engine mints). It also accepts exactly one workspace File access target, `/settings/workspace?workspace=NAME#file-access`, where `NAME` is an engine-accepted workspace name (`^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`). `parseWorkspaceFileAccessHref` exports that parse for `SettingsView`, and the whole address is validated: one `workspace` parameter, the literal `#file-access` fragment, no extra or duplicate parameter, no other path, hash or origin. On arrival `SettingsView` expands that row, scrolls the settings pane itself (measured from its `scrollTop`, never `scrollIntoView`), focuses the File access select with `preventScroll`, and briefly tints the row. Reduced motion drops the tint and keeps the focus ring. This is navigation only: it never chooses or saves a value, issues no write, and never switches the active workspace. A missing workspace is named in a status that says nothing changed; a different row with unsaved edits stays open, its single name draft untouched, and the status asks the user to save or discard before the linked workspace is opened. A load failure stays the tab's error, not a missing-workspace claim. Renderers leave those anchors without `target="_blank"`, and the delegated handlers (`ChatPanel`, `PinnedFilePanel`, `FileViewerModal`) push the route in the same window, so Back and an unsent draft survive. A plain click on a task address (`/tasks/<id>`) from a chat, a pinned file, the file viewer or the delegated-chat note opens that task's full editor over the current view through `ItemPeekHost.vue` and `stores/itemPeek.ts`, with no URL change and no history entry; on `/tasks` routes, and for a task already open on the board, it pushes as before. The overlay is the board's own editor: a close, a route change or a workspace switch awaits `requestClose()`, so a blank title, a refused or incomplete save, or a busy nested sheet cancels the action and leaves the draft on screen. `parseTaskHref` and `isPlainLeftClick` in `lib/appLinks.ts` are the shared parse and modifier check. Modified clicks and the middle button stay the browser's (new tab/window), and a dirty dialog's refused close cancels the navigation. Every other href — an unknown path, an extra query, a different hash, `/api`, `/ws`, a protocol-relative or external URL — stays an ordinary link in a new tab. File links are separate: opening a file still goes through `data-file-path` to the viewer, not through a page address.
- Chat Markdown tables use the renderer's `.markdown-table-scroll` region so compact tables shrink-wrap and wide tables scroll independently at narrow widths. Keep the region keyboard focusable and preserve readable key columns.
- DOM manipulation that needs to bypass Vue's scoped attribute (e.g. inline highlight spans inserted into rendered markdown) uses `:deep(...)` in the scoped stylesheet.
- **`ChatPanel.vue` ownership boundary.** The panel is being split in
  behaviour-preserving steps; put new work on the right side of the line.
  `composables/useChatComposer.ts` owns the composer — the draft and its
  synchronous persistence, prompt-history recall, textarea auto-sizing, caret
  insertion, and every attachment path (paste, drop, the native desktop drop
  grant, the image picker). It deliberately imports no store and registers no
  lifecycle hook: ids arrive as getters, the store arrives as the
  `ComposerAttachmentStore` interface, and `fetch` is injectable, so
  `composables/useChatComposer.test.ts` exercises all of it without mounting
  anything. `ChatTurnActivity.vue` owns the rendering of one completed turn's
  `Activity` disclosure and owns no state — open/closed, the thinking
  preference and the markdown renderer are props, and every action is an emit.
  `ChatPanel` keeps the send path, the slash-command and @-mention pickers,
  the trace open/closed map, the memoised markdown cache, scroll anchoring and
  the live streaming trace. Trace CSS lives in `components/chatTrace.css` and
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
  comment store, and the six `localStorage`
  keys behind them (`ciao-pending-images`, `ciao-pending-comments`,
  `ciao-pending-chat-comments`, `ciao-file-comments`, `ciao-pinned-files`,
  `ciao-dismissed-auto-pins`). **Chat pins are server-owned** (#1119): a chat's
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
  **A suggested command is engine-owned and transient** (#1843): a chat's
  offer is `{id, command}` in `ChatInfo.terminal_offer`, in the
  `chat_terminal_offer_changed` event and in the `/ws/events` snapshot's
  `chat_terminal_offers` map, and the browser keeps it in
  `stores/projects.ts`'s `terminalOffers` — in memory only, never written to
  `localStorage` or `sessionStorage`. The next chat read refills it under the
  read-start watermark, and the `/ws/events` snapshot replaces the map
  wholesale and itself becomes a watermark later reads must beat. A reload
  therefore refills from `GET /api/chats` even with the socket blocked. The
  card reads the map rather than a chat row, because a row's copy is only the
  payload the list was read at, while the map is the live one. Every read
  takes its watermark *before* the request (`terminalOfferReadStart()`) and
  may only write when no newer change or snapshot exists, so a payload that
  raced an offer change cannot roll it back. `dismissTerminalOffer(chatId,
  expectedId)` sends exactly `{terminal_offer:{expected_id}}` — never a pin,
  a title or any other update — and answers `dismissed` or `conflict`; a
  conflict adopts the engine's newer offer and the card says the suggestion
  changed. The card never executes the command, and *Open in Terminal* starts
  no shell, asks for no grant and asks the engine to run nothing. It must
  never be keyed by the offer id: a replacement patches it in place, so the
  notice that explains the swap survives.
  Pure logic the store hands over whole lives in `lib/`, Vue-free and directly
  testable: `lib/chatHistory.ts` (history normalising, server-row mapping, turn
  grouping, metadata merge, superseded live-tail pruning, tool icons),
  `lib/chatQuestions.ts` (AskUserQuestion and capability-question parsing plus
  the picker signature), `lib/chatWs.ts` (per-chat reconnect policy) and
  `lib/safeList.ts`. The three names that used to be exported from
  `stores/projects.ts` itself — `shouldReconnectActiveChatOnStreamingStarted`,
  `chatWsReconnectDelayMs` and
  `setListIndex` — are re-exported from there, so importers do not move.
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
  existing viewer, never a forced split pane or an auto-opened modal. A remote
  unpin removes the opener and, if its clean shared-pin preview is open, closes
  that preview — a separately opened viewer or a dirty edit is left untouched.
- **Workspace > Overview (`/settings/workspace`).** The first item of the
  workspace rail group is the page for `projectStore.activeWorkspace`:
  `SettingsWorkspaceOverview.vue` draws the Workspace, Chats and models and
  Google account cards (one editor, one Save changes / Discard row, in the last
  form card, Chats and models), then Memory handover, Update
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
  by default), Start at sign-in and Open
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
  copying it when the terminal is locked or unavailable. `composables/useMcpServers.ts`
  owns status and usage reads, while `components/settings/SettingsMcpServers.vue`
  renders provider configuration and per-chat connection states.
  Settings never edits native MCP configuration, handles secret values, probes
  servers, or starts a provider/MCP process; commands run only in the terminal
  the user is watching. The selected workspace scopes
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
- Completed chat traces stay collapsed as one compact `Activity` row. Touched-file chips sit below the final answer under `Outputs` (including files created via `Write` or common Bash redirects/`touch`/`cp`); interrupted turns keep their file chips inside `Activity` so unfinished work remains visible. Newly created files are labelled `new` on the chip.
- Conversation forks are initiated from the final assistant reply action group (Copy/Fork). The PWA sends the selected message slice up to that reply and redirects to the newly created chat, focusing the composer.
- New PWA actions (state-changing routes) must be documented in `../PWA_API.md` → Agent recipes, or whitelisted in `../tests/test_pwa_api_docs.py`.
- **The embedded terminal screen** (`lib/terminalRenderer.ts`) is a Vue-free primitive that owns one physical xterm bound to one session. The rules it keeps, and why: the session id is fixed at construction and there is no way to rebind it, because a screen that showed two sessions would be two histories at once; xterm's options object never leaves the module, so input is switched only by `setInputEnabled(boolean)` and starts off — a screen nobody has claimed sends nothing; a write is acknowledged only from xterm's own write callback, and the number it carries is the absolute end offset of the bytes just parsed, so a resume can use it and nothing else; a write that is still queued or in flight when the screen is disposed is **rejected, never resolved**, so a disposal never advances an acknowledged offset and the bytes come back on the next replay; the theme is read from the app's own tokens and resolved to the `rgb()`/`rgba()` values xterm can parse, and a `color-mix()` token is refused rather than passed on; one ResizeObserver, owned by the renderer, measures the screen and is disconnected on dispose. xterm and its fit addon are imported dynamically, so neither enters the bundle of a page that shows no terminal — Home's payload is unchanged by the primitive existing. The pane that shows a screen is not in this repository yet.

- **The notification inbox is a derived view, not a source of truth.** The inbox, opened from the bell in `components/NotificationBell.vue` (body in `components/NotificationInboxList.vue`), projects four sources, keyed `chats`, `operator`, `automations` and `updateTasks`. `chats` is the `projects` chat predicates plus `taskSignals` board review, and carries board-result and memory-insight rows; `operator` is `housekeeping.actions`, package and provider updates included, with `github-star` excluded; `automations` is `tasks.schedules` through `scheduleInWorkspace` and `scheduleNeedsAttention`; `updateTasks` is `housekeeping.updateTasks` through the shared `lib/updateTasks.ts` predicate that Home's strip also uses. `stores/notificationInbox.ts` only adapts those owners and tracks health, and `lib/notificationInbox.ts` is the pure projection. The count is distinct rows, not messages: a chat that needs the user and is unread is one *Needs you* row with an unread marker, a board result owns its review row, and an automation incident suppresses its linked failed chat. `99+` is visual only; the accessible name carries the full number. Reading is not resolving: opening the inbox marks nothing read, opening a chat clears unread through the existing `chat_read` lifecycle, and a pending question or permission stays a row. There is no dismiss, snooze or mark-all-read; resolve at the source. Exact links go to `/chat/<id>`, `/tasks/<id>`, `/schedules/<id>`, `/settings` or `/settings/providers`, and to `/?notice=<key>` for other operator notices and update tasks. A chat, task or automation whose target has gone re-reads, shows *That item is no longer available* and navigates nowhere. Freshness is per source (`checking`, `ready`, `stale`, `unavailable`); the worst one is the overall status, and an empty list is all clear only when every source is `ready`. Retry calls the owners' read methods only. `init()` runs from `ChatLayout` after bootstrap and `dispose()` on unmount; it installs one focus/visibility listener with a 5-second throttle and a scoped set of watchers, has no timer, and rejects late work with a generation token after a workspace switch. A new source gets a key in `INBOX_SOURCES` and a builder in the store's snapshot table, is seeded in every all-clear test fixture, and never gets a mutation path.

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
| `narrow-viewport.spec.ts` | Layout at 390px. jsdom has no layout engine: every rect is 0x0 and `scrollWidth` is always 0, so neither the unbreakable-flex-child trap nor a tap target under `--touch: 44px` is visible from a mount. The memory-insight journey also measures the row that carries a second control (the retry beside the open control), which is the one place a row can grow past the pane. Selecting a reply measures the opposite case: the action footer is not in the layout until the message is selected, and selection must leave the transcript's `scrollTop` alone; the fixture's opt-in `comment` shape then measures that the selected card's accent outline does not bleed across the turn gap onto the comment-reference card below it. |
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
the "After this update" group, which is empty by default because the Home gap
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
npm run test:e2e                  # rebuilds the PWA, then runs the suite
npm run test:e2e -- --headed      # watch it
npm run test:e2e -- --ui          # pick and step through a spec
```

`@playwright/test` is pinned exactly in `package.json`; the browser binary is
downloaded separately, so a contributor who never runs this suite pays only the
package (~18 MB of `node_modules`), not the browser.

CI runs it as a blocking step of the existing `test` job, straight after
`npm run build`, reusing that job's install and build output.

### Still manual

Browser automation does not reproduce iOS PWA suspension, the software
keyboard, or native-shell behaviour. The device checklist in "iOS PWA gotchas"
above still has to be walked by hand on a real phone before a release.

The MCP Settings panel shows provider-native configuration separately from per-chat connection states returned by `/api/mcp/status`. No running observable session means connection not checked, and unreadable configuration/provider status is unknown.

Onboarding's care stage (`components/onboarding/OnboardingCareStage.vue`) asks when the host is usually available, then lets the operator edit the installed workspace-care routine's local time and IANA timezone. It updates only that existing row through the schedules API; keeping the packaged default writes nothing. No routine is created when the workspace has none.

- The To decide filter chips are by change type, by reason, and *Needs you* (`decideFilter` in the proposals store, separate from `kindFilter` because History shares that one). *Needs you* keeps rows whose `decider` is not `nightly`, so rows with no decider count, and the chip shows only when at least one row is marked `nightly`. A row shows its `decider` and a one-line `decide_reason`, rendered as text.
- An optional reason is asked for before every individual accept and dismissal in the queue: the row's accept opens the decision card with the reason on it (`Why keep this? (optional)`), and every Dismiss opens an inline confirmation naming the proposal (`Why dismiss this? (optional)`). Picking the workspace for a re-home is the exception — the destination is the answer — and the batch bar never asks. There is no *Why?* disclosure left on a row or a card. The shared `DecisionReason` with the memory chips from `lib/memoryDecisionReasons.ts` holds the answer per row in the panel (`reasonById`), and it is discarded on Cancel, on a switch to another card, and after a successful decision; a failed dismissal keeps it so the retry does not lose the sentence. It is sent as `reason` only when non-empty, with that row's accept (and the retry of a deferred accept) or its dismissal, never on the batch bar; a refused accept clears it, the deferral keeping its own copy for the retry. With no reason typed the confirmation's button reads *Continue without a reason* and the card names the action above it (*This will save to ciao:memory.*, *This will dismiss the proposal.*); with a reason the accept button names the destination again. History renders a row's `reason` as text (`.ph-reason`), never markup.
- History shows a row's `prediction` under its text as predicted against decided, with a muted *Matched* or *Missed* badge (a miss is information, never an error) and the prediction `reason` as text. A `[memory]`/`[profile]` row the nightly run settled itself (`via: agent`) reads *Filed by Workspace care* (accepted) or *Dismissed by Workspace care* (dismissed), badges *Acted on prediction*, and offers *Undo* from the row when its change is undoable. The To decide queue never shows a prediction on a pending row, so the agreement rate stays unbiased.
- The facts inside a note (`VaultReviewPanel.vue`) are interpolated prose (`<p class="vr-entry-text">`), never `<pre><code>` and never `v-html`: the excerpt is capped and marker-stripped in `ciao/memory_audit.py`, and a note's own text stays literal. The section is a muted line of its own, so the fact's heading is not repeated in the grey context; `context` holds at most one same-section plain-prose line either side of the fact, and only lines `ciao/note_entries.py` classified as paragraph prose qualify: a thematic break, a table row, a quote, a fenced block, the frontmatter or a sibling bullet is skipped rather than quoted. A whole GFM table block is excluded by a display-only pass (`_table_line_flags`) after the walk, so sections and identities are untouched. Every heading the parser recorded is a hard boundary, including one separated by blank lines or several sibling bullets.
- Settings → Overview → *Memory handover* (`SettingsMemoryHandover.vue`, a three-way segmented control styled like Theme: arrows select) loads `GET /api/proposals/handover?workspace=` for the active workspace and saves only `{ memory_handover }` through `PATCH /api/workspaces/{name}`. New workspaces start at *Dismiss and accept*; existing workspaces without a stored choice remain *Off*. It never saves on its own: the suggestion is text. Choosing *Dismiss and accept* asks for confirmation, and the card says that it lets queued text reach always-loaded memory, with Undo in History and a cap of 5 changes per run. When the response's `acting_available` is false (a Linux/systemd install, where the gate trusts no service definition) the card says handover cannot act on this install and disables the options.

### Onboarding stages

`components/onboarding/` holds presentation stages that the shell composes; each receives
`OnboardingStageProps` (`context`, `busy`) and emits `state`, `continue`, `back` and `refresh`.
A stage owns its domain save and reports whether it succeeded; the shell, never the stage, writes
progress through the onboarding store.

The onboarding location stage owns the folder-first location choice and workspace name validation; it reports readiness to the shared shell and emits `continue` only on the user's action. It never writes onboarding progress. Provisioned installs treat the current folder as read-only and link to the guarded workspace move flow.
The onboarding finish stage shows a summary built only from observed values and offers an optional
first task that creates a chat and sends the submitted text; it does not change setup progress
itself.

`OnboardingRuntimeStage` reads model provenance from
`/api/models`: runtime-reported models and configured aliases are rendered in separate sections,
and an unobserved runtime is never described as available.

## Onboarding memory

The Access & memory onboarding stage asks workspace operators to acknowledge a handover choice before continuing. Its suggested “Accept and dismiss” choice applies only while that workspace's onboarding receipt is `in_progress`; it is never written until Continue, and is saved only through the workspace's existing `memory_handover` setting. A missing trusted handover service is reported plainly and does not imply acceptance can act.

### Onboarding entry

`/onboarding` composes the stage components by the receipt's recorded step and advances progress only
through the onboarding store; `OnboardingView` never writes progress itself, and the access/memory
screen advances only when both components report ready. `HomeOnboardingResume` reads the current
receipt on Home entry and window focus and links into the journey while a target is pending — it is
not a dismissal, and it shows nothing when the receipt cannot be read.
