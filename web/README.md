# PWA frontend

The sidebar's **Search chats** dialog searches saved titles and visible transcript text in the selected workspace. It sends the query only in a bounded JSON body, shows literal plain-text excerpts (markdown flattened by `lib/searchText.ts`, query terms marked by splitting text, never as HTML), lists results as a combobox listbox (Up/Down select, Enter opens), shows the project and an *Archived* chip on each row, and opens archived results through the existing read-only chat flow. Search results are fetched a page at a time.

Vue 3 + Vite + Pinia + TypeScript. Built output goes to `ciao/web/static/`, served by the same Starlette server as the API. See `../README.md` for the repo-wide layout and `../PWA_API.md` for backend routes.

The file viewer is Vue-first. Text, Markdown, CSV, PDF/PPTX (via a binary preview), and HTML artifacts all render through native Vue components (`HtmlArtifactViewer.vue`, `CsvViewer.vue`, plus text/iframe previews). Diagrams live inside HTML artifacts as inline SVG; `.excalidraw` is no longer a viewer type. History and Diff operate on the raw file snapshots.

Chat-originated file opens resolve a canonical absolute path through
`/api/chats/{chat_id}/file-path` before rendering or pinning. Relative paths are
anchored to that chat's agent root, never the sidebar's selected workspace.
Pins report success explicitly; failed pins keep the modal open, and an older
pin request cannot close a newer file preview.

Completed final assistant bubbles also link an untouched mention (a filename or relative path the reply names that no tool touched) only after `POST /api/chats/{chat_id}/file-paths/resolve` confirms it (`composables/useFileMentions.ts`). The lookup is browser-only and bounded per generation: 100 examined message revisions, 262,144 content characters, 200 candidates and 20 requests, in batches of 50 items or 32 KiB. It covers only the history already loaded, and it never searches by basename. A missing, ambiguous, failed or over-budget mention stays plain, with no toast. Discovery pauses while streaming. A context change clears the confirmed links at once; a finished turn, a reopened chat or `invalidate()` starts a new generation and keeps the links until its answer arrives. Only vault-relative or in-vault absolute spellings link; an install-relative spelling stays plain. Explicit Markdown links, tool-touched paths and the viewer policy are unchanged.

The chat panel and pinned-file panel load when their views need them; a direct
chat link starts the chat download alongside bootstrap data. The
file viewer loads on first open and stays mounted afterward so dialog focus
restoration and edit state keep their normal lifecycle. The item peek host
(`ItemPeekHost.vue`, which mounts `TaskWorkspaceSurface.vue` in its overlay
mode) loads the same way, on the first task link opened over a chat; Back
closes it through `useOverlayBackEntry`. Home does
not fetch
these optional modules. The chat rise starts when the real main chat panel
has mounted and the route has reached that chat, not when the chat is selected,
so a slow chunk leaves the skeleton unmarked and the panel rises when it arrives.
A navigation elsewhere, or one the router refuses, cancels the pending rise. The engine status request is bounded and shared between
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

Automatic quota retries show the persisted next-attempt date and time in local
time. Confirmed Claude reset times are used when valid; recognized quota errors
without a trustworthy reset keep the hourly schedule.

Production hashed JavaScript and CSS under `/assets` use negotiated gzip above
500 bytes. Other static files, API responses, and chat streams keep their
existing response path. Conditional and ranged asset requests retain their
identity representation.

The chat rail uses Option/Alt+Left and Right to move through the current workspace's chats, including from the composer. Matching chords are consumed at the ends; the title's unmodified arrow-key navigation remains available. Configured Option shortcuts work in editable fields, including keys reported as dead keys; unbound dead keys, IME composition, AltGr, unmodified workspace digits and controls that claim their keys retain precedence. Explicit custom and disabled bindings are preserved.

## Dev workflow

The Repository section in Chat details reads `/api/chats/{chat_id}/worktree-changes` only after **View changes** is opened; **Refresh** is explicit. It shows staged and unstaged patches, untracked filenames, a clean state, unavailable and retry states, and a truncation notice. A response is discarded when its chat or checkout changes. The view is read-only and uses Vue text interpolation for paths and patches.

The embedded LoginView keeps terminal guidance and the six-item product tour for the CLI bootstrap.

Shell permission cards display the provider's exact bounded command and its
validated working directory (or **Unknown** when omitted). The command is kept
only in the live stream for reconnect replay; approval actions are at least
44px high and the command block can be focused and scrolled by keyboard.
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
`web/dist/storybook` output. It imports production components and CSS directly
and does not load the app bootstrap or public assets. `npm test` remains the
separate application suite.

**Coverage rules.**

- **Covered** means a rendered-state story exists, not that interaction is verified.
  A story does not imply connected-caller, loading-fidelity, keyboard, reduced-motion,
  responsive or migration coverage.
- Stories preserve current production behaviour, even where it differs from the
  desired contract. Record the difference in the story; do not fix it there.

**Connected fixtures.** A story that renders an owner which transports data declares
what it may reach; nothing else is reachable.

- `src/stories/fixtureBoundary.ts`: `installFixtureBoundary({ routes: [{ method,
  path, respond }], sockets: [{ path, script }] })`. Only exact declared reads and
  writes are answered in memory (path is pathname + search + hash; a body on GET or
  HEAD is a violation). Every other fetch, new XMLHttpRequest, WebSocket,
  EventSource or beacon is rejected with no native fallback; existing XHR open/send
  and WebSocket send stay fenced.
- A route whose `respond()` never resolves holds the request until teardown.
  Teardown unmounts the isolated app, disposes its Pinia, settles held requests and
  restores transport descriptors. The boundary mounts once: concurrent or
  inline-docs mounts are refused, and connected owners stay excluded from autodocs.
- It is a trusted-component harness, not a security sandbox. DOM resource loads,
  workers and native references saved outside the fixture are not fenced.
- `src/stories/mountConnected.ts`: `mountConnected(host, { component, routes,
  routerRoutes, seed })` mounts the owner into a separate Vue app with a fresh memory
  router and fresh Pinia. It seeds store state only, never app orchestration, and
  starts no engine, provider or bootstrap.
- Declared app sockets never open natively. The story gets an in-memory fake (send,
  close, readyState, the ready-state constants, `instanceof WebSocket`) whose far end
  is its `script` (`serverOpen`, `serverMessage`, `serverError`, `serverClose`).
  Deliveries flush on the next microtask, after the component assigned its handlers;
  a faulty script fails construction. Declared sockets must be same-origin at an
  exact path; anything else, including the engine's `/ws/events`, is refused.
- The preview decorator installs the boundary for every story and docs page from
  `parameters.fixture`, a typed `{ routes, sockets }` config (empty when absent).
  There is no skip flag. A connected story declares its routes there, e.g.
  `parameters: { fixture: { routes: statsRoutes('default') } }`. `mountConnected`
  mounts into the boundary the preview built and throws when none is active.
- Duplicate routes or socket paths, a malformed config and a non-array routes list
  are `TypeError`s. Docs mode shares one empty boundary across stories, so a docs
  story that declares routes or sockets is refused.
- Violations are asserted at teardown: the preview's global `afterEach` fails the
  story when the boundary recorded any violation, even if the component swallowed
  the error (`fetch(...).catch(() => {})`, a try/catch around XHR, WebSocket or
  beacon). Play functions assert the violation ledger stays empty.
- Owners with singleton request state (for example `askConfirm`, `askPrompt`) cancel
  outstanding requests on teardown and are excluded from inline Docs.
- Not fenced: `localStorage`, `sessionStorage`, IndexedDB and service workers. A
  story that touches them is not isolated by this boundary.
- The only native sends allowed are: Vite's HMR ping on its established same-origin
  `vite-hmr` socket; render lifecycle and heartbeat events plus preview keyboard
  reports (`previewKeydown`) on `/storybook-server-channel` with its exact preview
  page and token; and, only under `npm run test-storybook`, the vitest browser
  runner's control socket (`/__vitest_browser_api__`) for birpc requests, events
  (no `i`) and optional calls (`o:true`) whose method is on the runner's page-side
  allow-list, plus responses. Custom, invoke and save events are not allowed.
  Module and asset loading is infrastructure, not an API fixture.
- Use synthetic fixture names (`storybook-alpha`, `storybook-beta`). No real paths,
  tokens or user text.

**Checks.**

- `npm run build-storybook` writes the static workshop; a story that fails to build fails the command.
- `npm run test-storybook` runs the browser interaction runner (@storybook/addon-vitest,
  vitest browser mode with the Playwright provider): every story renders in headless
  Chromium and its `play` functions execute there. Chromium must be installed locally
  (`npx playwright install chromium`).
- Node tests check data and filters, not Vue interactions or manager links.

**Visual check.** Filtering, keyboard access, narrow and zoom layouts and story links
are checked by hand in both the development (`npm run storybook`) and built workshop,
in a browser. Record screenshots and network evidence with the change. Use 390px and
1280px, dark and light (once the appearance toolbar exists), and 200% zoom for one
story; check keyboard focus and 44px touch targets. A 390px container is not a
narrow viewport: still verify an actual narrow viewport and browser zoom.

After changes that touch running static assets (anything that lands in `ciao/web/static/`), redeploy via the PWA's Settings → Deploy. **Do not restart the ciao service yourself**: you'd sever the PWA session the agent is talking through.

## Layout

The expanded sidebar places the six destinations above one contextual scroller (including Settings and Memory filters), with workspace selection in a fixed footer. Its upward menu is bounded by the visible viewport; an exceptionally short sidebar lets the header scroll to preserve access to the footer.

Onboarding's Access stage chooses the runtime approval mode and states, with the agent CLI's permission-docs link, that bypass is full user access (`lib/permissionDocs.ts`). It writes only explicit changes through workspace settings; an unset permission mode remains unknown/engine default and is never presented as a known guarantee.

```
web/
  index.html              entry HTML, viewport meta, PWA manifest link
  src/
    main.ts               Vue bootstrap + iOS viewport / keyboard / zoom plumbing
    App.vue               root component; loads the global stylesheet
    styles/app.css        global CSS tokens (--bg, --fg, --accent; `data-text-contrast="soft"` on `<html>` lowers --fg; `data-body-font` swaps --font), caret, shared focus utilities
    router.ts             routes: /login, /, /chat/:id, /project/:id, /schedules, /tasks,
                           /memory, /stats, /settings, /settings/:tab
    components/           one Vue SFC per feature pane (including HomeIntake, HomeReviewSummary, UsageView (the /stats Usage page; data from lib/usageStats.ts),
                           CommandPaletteModal, FileViewerModal, NotificationBell (the inbox bell and its popover or phone sheet), NotificationInboxList (the inbox body), HomeNoticeWindow, HousekeepingStrip (Home's single "Needs you" list above the composer: workspace-health errors and operator notices, one title, one line and one primary button per row) with NeedsYouRow (the row and its ⋯ menu), and WebhookTriggers)
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
                           SettingsEngineLogin.vue; SettingsWorkspaceOverview.vue is the Workspace > Overview page;
                           SettingsWorkspaceModels.vue is its Chats and models card;
                           SettingsClaudeTrim.vue is its Claude context trimming card (catalog from the engine, pure helpers in lib/claudeTrim.ts; edits the same draft, saved by Chats and models' Save changes);
                           SettingsAllWorkspaces.vue is Overview's last card: boxed
                           rows, the short New workspace form and Archived; rows reorder by
                           drag, Alt/Option+Up/Down on the handle, or Move up / Move down in
                           the row menu, through `store.reorderWorkspaces`, with the polite
                           announcement text from `lib/workspaceOrder.ts` (these rows keep their own native drag handle and do not use `useDragSort`); WorkspaceColorPicker.vue is the accent
                           swatch group both use: five base presets plus a dashed "+" disclosure for
                           blue, teal, lime and fuchsia), plus the
                           scoped CSS they share with it
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
                           `kindLabel`, and is cross-checked against the care and memory-pass prompts
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
                          useModalFocus, useOverlayBackEntry (one history entry per open
                          overlay; Back closes it), useWorkspaceAdmin, useProviderChoices,
                          useHomeNoticeWindows, useDragSort: the pointer drag-sort engine
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
    lib/                  pure helpers (api, time, safeMarkdown, contrast, chatActions, permissionModes, notificationInbox, sourceHealth,
                          etc.) — no Vue imports; `taskOrder.ts` (the server order, lane derivation,
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
browser tab cannot know whether the app is installed on the device. Inside Ciaobot for Mac, which sets `window.__CIAOBOT_DESKTOP__` (`isDesktopClient()` in `lib/pwaPlatform.ts`), the install card is not shown, the Home setup reminder is not shown, and the notifications card mirrors the menu bar's Settings → Chat Notifications row through the `ciaobotAppSettings` bridge (`lib/macAppSettings.ts`, the only reader of that handler): a switch drives the same row, or a button opens System Settings when macOS blocks it, and the card re-reads on the app's `ciaobot-app-settings-changed` event. A Mac app without the bridge keeps the static "Handled by the Mac app" text. The Updates card leads with a **Ciaobot for Mac** row (`components/settings/SettingsMacUpdates.vue`): the app's status, a switch for Install Updates Automatically and a Check for Updates… button, the same actions as the tray. When updates are unavailable the native reason is shown as text and both controls are disabled. A Mac app without the Updates block shows no such row and keeps the engine's own text.

The one-time *Your Ciaobot* card on Home (`components/HomeWelcomeCard.vue`,
state in `composables/useWelcome.ts`) shows the workspace folder, the generated
password and the AI tool while the engine's `.runtime/welcome-pending` marker
exists. The password is fetched only when the user presses Show or Copy, lives
only in the component's state and is cleared on dismiss and unmount. Only the
engine decides `on_host` (from `GET /api/welcome`); the PWA never infers it.
The X dismisses engine-wide through `/api/welcome/dismiss`, not per browser.

The *Switch to the new Ciaobot app* row in *Needs you* is an engine operator
action (`mac-app-switch`, `ciao/operator_actions.py`): only an installer-managed
engine on an Apple-silicon Mac emits it, so a Linux engine, the app's bundled
engine and a checkout never do, and the PWA never decides its eligibility. The
PWA does not filter the row either: a 0.5.x Mac app user viewing their old
engine's PWA is exactly its audience, so the row shows there too. *Not now*
snoozes it engine-wide through `/api/housekeeping/mac-app-switch/dismiss` for 30
days, and the *Download for Mac* link is `MAC_APP_DOWNLOAD_URL` in
`ciao/package_version.py`.

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
- Fixed phone sheets that must remain reachable above the keyboard use `--app-h` (falling back to `100dvh`) for their available height, rather than assuming the full screen is visible.
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

### Chat permission mode picker

The normal chat composer model picker changes the saved permission mode for that chat through the existing chat update API. Changes apply to subsequent turns, not a reply already running; queued or external turns may start before the write settles. While pending, ordinary sends from that composer are paused without clearing the draft, while `/mcp`, Stop and recovery remain available. `touch-targets.spec.ts` covers the picker at constrained viewport sizes and touch targets; component coverage lives in `ChatPanelRetry.test.ts` and `ModelSelector.test.ts`.

An attended shell permission card displays the exact current command submitted by the provider, bounded at 4096 characters. It stays unformatted, pre-wrapped and keyboard-scrollable so multiline and JSON-looking commands remain literal on mobile. Approve and Deny are distinct controls with 44px minimum height; Approve grants only once. The full command is held by the current stream for authenticated reconnect replay, not persisted in the chat registry. A shell attention marker alone never rebuilds an actionable card, and engine startup clears stale markers.

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
- **Brand mark and wordmark**: `BrandMark.vue` renders the Ciaobot C as inline SVG at 30×30px: the C follows `currentColor` and its two dots use the selected `--accent`. `CiaoMark.vue` shares that vector mark in Settings (static there) and takes an optional `progress` prop (0 resting C, 1 closed ring; geometry in `lib/ciaoRing.ts`, shared with the website header). `BrandMark` is a button: hover or focus closes the C into a ring at a slow follow rate and leaving reverses it from wherever it is, reduced motion keeps the static C, and a click hard-refreshes (service workers updated, caches cleared, reload with `_r`). Only the expanded sidebar owns the navigation mark. `PaneHeader.vue` has no mark or `brand` prop; it retains page tags, title/actions slots and mobile controls. A collapsed sidebar or closed phone drawer needs no logo: its toggle or hamburger still opens navigation. `Wordmark.vue` renders the supplied wordmark image (`web/public/brand/`): ink on light themes, off-white on dark ones, switched by the `theme-light` class. It is used by StartupView, UpdateProgressView, LoginView and RestartNotice.
- **Caret**: `.caret` is a blinking accent terminal caret. Pair it with the wordmark for "live" surfaces (login prompt, idle empty state).
- **Body and scroll:** the normal app surface is clean (no CRT grain); startup/update/native surfaces retain the terminal texture. Scrollbars are thin and visible so long files, tables, tabs, and settings panes never hide the fact that more content exists. The chat footer's turn-facts strip is the one exception: it scrolls sideways without a visible bar, because a bar under a reply reads as a rule across the transcript.

Shared utility classes (defined globally in `src/styles/app.css`): `.btn-primary`, `.btn-small`, `.btn-icon`, `.btn-chip`, `.badge` (with `--accent|--accent2|--muted|--success|--warn|--error|--dot`), `.page`, `.card`, `.form-grid`, `.form-group`, `.form-actions`, `.hint`, `.checkbox-pill`, `.modal-backdrop`, `.modal-sheet`, `.sr-only`. Modal-like surfaces use `composables/useModalFocus.ts` for initial focus, Tab containment, Escape capture, inert background content, and opener restoration; do not add a second window-level Enter shortcut that can confirm while Cancel is focused.

Prefer the utility classes over re-inventing the same button/badge/card per component.

## Conventions

- **Headless primitives migration.** `ConfirmDialog.vue`, `PromptDialog.vue`, `NewChatPicker.vue`, and `FileViewerModal.vue` use `reka-ui`'s unstyled Dialog primitives for modal semantics, focus trapping, Escape/outside dismissal, and focus restoration while keeping token-based markup, themes, and 44px controls. `CommentComposePopover.vue` keeps its app-specific Teleport, dismissal, and visual-viewport placement, but uses a Reka focus scope so it composes safely inside the file-viewer dialog. `ModelSelector.vue` uses a non-modal Reka Popover (with its searchable listbox kept inside the popover) and delegates fixed collision placement to Reka's Popper, including the triggerless mobile sheet. `ProjectSidebar.vue` uses Reka DropdownMenu roots, items, and submenus: the visible action buttons provide keyboard/touch entry points, while a right-click supplies a virtual pointer reference. The hover-preview read surfaces in `ChatCommentPopover.vue` and `PinnedFilePanel.vue` deliberately use a non-trapping, app-specific `FocusScope` plus measured positioning instead of Popover because hover must not steal focus and a click can pin the surface. The file viewer's internal read-comment popup uses a trapped nested `FocusScope` because it is a dialog-in-dialog with its own Escape and focus-return boundary. These bridges keep hover/pin state, local dismissal, focus return, and visual-viewport clamping; do not replace them with a focus-stealing modal primitive without changing that contract. Use `@vueuse/core` for small browser-state helpers when it removes custom lifecycle code; keep iOS visual-viewport measurement and clamping in `lib/viewport.ts` and `composables/useViewportHeight.ts`. Prefer Reka for new accessible overlays, retain hand-rolled code for app-specific layout or measurement, and do not add Tailwind or a themed UI kit. **A modal drawn inside a pane must put its `DialogOverlay` in a `DialogPortal`**: `.chat-main` declares `container-type: inline-size`, which makes it the containing block for `position: fixed`, so an in-place overlay dims and clips to the pane and leaves the sidebar live under a "modal" (`WebhookTriggers.vue` does this; `PromptDialog`/`ConfirmDialog` get it by living at the app root). Reka unmounts a closed dialog's content, so tests read a portalled dialog through `document`, never through `wrapper`.
- One Vue SFC per pane. Keep `<script setup lang="ts">`, template, scoped `<style>`.
- **Webhook triggers live in Automations**, not in a top-level inbox (#1034). `stores/webhooks.ts` owns one workspace's triggers through the same B4 load contract as `taskBoard` (`loadedWorkspace`, `loading`, `loadError`, `error`, a `requestSeq` read ticket and a `writeSeq` write ticket), and every write presents the `revision` the row was read at — a stale write is the server's 409 with the rows intact. **`mode`, `project_id` and `input_policy` are create-only**; `PATCH` accepts `name`, `instructions` and `enabled` and nothing else. **The store never holds a secret**: `create` and `rotate` return the one-time secret to their caller and no field keeps it, because `WebhookTriggers.vue` is the only surface allowed to show it, once. The receiver recipe (`lib/webhooks.ts`) carries the trigger's real id and a `<secret>` placeholder, never a credential — a recipe that outlived the copy button is not a place to keep one. The row, heading and section classes come from `components/overviewSections.css`, which both `SchedulePanel.vue` and the section pull in with `<style scoped src>`.
- Load states are separate states. A list that fetches (the Memory Map, the task board) must distinguish first-load in flight, first-load failure (inline error + Retry, never an empty-state claim), a failed refresh over existing rows (keep the rows, mark them stale, offer Retry), a filter hiding a non-empty set (offer to clear filters), and a genuinely empty set. Never derive "there is nothing here" from a filtered array alone — a failed or pending GET would then read as a cleared queue. Load errors live in their own store slot (`loadError`), apart from action errors (`error`), so a list refresh cannot clear an unread accept/dismiss failure. `useTaskStore` exposes the same contract for schedules through `scheduleLoading`, `scheduleLoadError`, and `schedulesLoaded`; Home, Project, and Automations consume that shared truth rather than maintaining competing local interpretations. The workspace task board's store is `taskBoard`, deliberately not a second half of `tasks`: schedules are automations the engine runs, task records are the user's own, and every write in the board presents the `revision` it read so a stale edit is the server's 409 with the rows intact rather than a lost update. Its pure half — column bucketing, ordering, the filters, and the `/api/tasks*` error envelope — is in `lib/taskBoard.ts`.
- Store reads are computed state, not setup-store functions: Pinia exposes returned functions as actions, and invoking one activates that store's Pinia. A scroll or render path that only reads state must use a computed value (for example, per-chat pagination maps), while operations that mutate state or perform I/O remain actions.
- The core work model is **request → run → output → durable knowledge**. `HomeIntake.vue` starts an ordinary project chat and preserves unsent text per workspace; `HomeReviewSummary.vue` uses active-workspace counts and labels checking/current/stale/empty/failed state explicitly and, on Home, is one docked window in the `.home-workbench` side rail beside the request column (stacking below it under a 940px pane), with `HomeTodayStats.vue`'s Today counts from `GET /api/stats` under its queues; `ChatPanel.vue` keeps the transcript dominant and opens a conditional, keyboard-operable Context / Activity / Output inspector. New UI should reinforce those relationships rather than introduce another top-level inbox or artifact silo.
- **A memory pass is not a chat row.** It is an app-owned chat in a hidden project, and it used to be listed twice for one archived conversation: once in Home's tiers under its own internal title (`Memory pass · …`), once as the conversation it works on. It is now one row, in the `memory insights` section `HomeRecentChats.vue` renders below the tiers, and the store filters a declared pass out of `activeChatsAll`, `projectChats`, `totalUnread`, the sidebar and every schedule target. Put the whole derivation — one row per archived conversation, the archive pipeline and the pass joined, every phase and its wording — in `lib/memoryInsights.ts`, not in the component; only the signals come from the store (`memoryInsightRows`). `ChatPanel.vue` names the source conversation above the transcript (and at the top of the Chat details rail, which shows it instead) and links the archived transcript, so an opened pass says what it is. `lib/memoryPass.ts` stays the only reader of the `memory_pass` helper kind, with the Memory project kind as the fallback for a helper that failed closed. The fallback is deliberately visible in `activeChatsAll` and `projectChats`: it has no memory-insight row, so its ordinary chat row is the only way back to its pending question. It is silent on every notification surface — toasts, the notification inbox, the bell and OS badge, the menu bar, `projectNeedsInput` and unread — even when blocked. The project kind is the app's own signal, so the engine keeps it to passes only: the Memory project holds the engine's own passes and nothing else, the create route refuses a client-supplied `memory_pass` helper, and the schedule and webhook pickers do not offer it as a target. A routine bound to it (or to a pass) is refused with a message that names the reason, and an existing one can still be paused or retargeted.
- Project, Memory, and file rows are native buttons or links whenever they perform work. Context menus use `role="menu"` / `role="menuitem"`, open focus on the first action, support Arrow/Home/End/Escape, and restore the trigger. Pointer-only rows and right-click-only actions are not acceptable.
- **Loop wake cards.** `lib/injectedPrompt.ts` draws a chat-owned loop's wake (origin `loop`) from the header `ciao/loop_wake.py` writes; a header it does not recognise is a plain bubble, never a throw. The card links the Automations list rather than one entry, because a loop is not stamped onto its chat.
- Markdown rendering goes through `lib/safeMarkdown.ts` (DOMPurify + marked + highlight.js). Never `v-html` raw user content. User chat bubbles use `renderUserMarkdown`, which escapes raw HTML (except the comment-context tags) so typed text shows as typed; assistant output uses `renderMarkdown`. `ChatPanel` passes `fileMentionTargets` only from the final reply bubble (`renderAssistantMarkdown`); no other surface opts in. A message that names a conversation the reader knows links to it under its human title (`lib/chatLinks.ts`): a bare id or the app's own `@chat/<id>` mention is linked, only for an id in the reader's own openable chat list, the raw id stays in the tooltip and in `data-chat-id`, and the label is built as text, so a title can never become markup. An image an assistant message embeds by workspace-relative path (`![shot](memory-vault/x.png)`) is served from `/api/workspace-image?path=…&chat_id=…`, which anchors the relative path to that chat's agent and vault roots (exact match, no fuzzy walk, so a message of made-up paths costs no directory walks; it anchors, it does not sandbox); left as written, the src resolved against the `/chat/...` route and loaded the app shell, a broken image. `ChatPanel` passes `{ chatId }` to `renderMarkdown` for it; web URLs and root paths (`/api/images/...`) stay as written.
- **After sanitization an anchor has a target or it is not an anchor.** The sanitizer drops a `file:`, `javascript:` or `data:` href, and the anchor used to survive it: still painted with the accent colour and an underline, still swallowing the click, pointing nowhere. `lib/safeMarkdown.ts` now renders such a link as inert text — a `<span class="markdown-dead-link">`, never an `<a>` — with the label and its inline formatting intact, and a file path kept visible as copyable text (a network path keeps its authority: `file://server/share` reads `//server/share`). The sanitizer's own URI policy decides (`DOMPurify.isValidAttribute`), so the two cannot drift apart, and the policy is not widened. One exception to the inert text: a local file path, in a surface that opted in and on the engine's own machine. A pane opts in by passing `{ openLocal: true }` to `renderMarkdown`/`renderUserMarkdown` (the chat bubbles in `ChatPanel`) or `renderVaultMarkdown(..., true)` (the file viewer and the pinned-file panel), and it may only ever opt in if its click chain acts on the button — the delegated `handleOpenLocalFileClick` handler. There the dead link becomes a real `<button class="markdown-dead-link--openable" data-local-path="...">` (the label and the visible, copyable path), and the handler asks the engine to open that path with the OS default application through `POST /api/workspace-open` (`lib/openWorkspaceFile.ts`). Every other markdown surface (`TaskNoteFeed`, `TaskWorkspaceSurface`, …) renders the dead local link as inert copyable text, because a clickable-looking control whose click goes nowhere is the very defect this bullet fixes. Off the engine's own machine — a phone or another computer on the LAN — the button is not rendered either, and the link stays inert text, because `/api/workspace-open` is loopback-gated on the TCP peer. `file://` navigation still never happens: there is no anchor left, and the engine request carries only the path. The neutralising pass runs last, after `linkifyHtml`, because that pass skips the content of an anchor and a dead link's label must not be turned into a workspace file link; it walks anchors only, so the openable button is left alone. This holds for `renderMarkdown`, `renderUserMarkdown` and `renderFileMarkdown` alike.
- **A link into the app navigates in place.** `lib/appLinks.ts` is the one allowlist: `/`, `/tasks`, `/schedules`, `/automations`, `/memory` and its map/categories/unsure sections, `/settings`, `/settings/home` and the tabs in `SETTINGS_NAV`, the documented anchors (`#install-app`, `/settings/providers#chat-providers`, `/settings/workspace#new-workspace`), one task record by its full 32-hex id, and one conversation by its own id (`/chat/chat-0123abcd`, the shape the engine mints). Renderers leave those anchors without `target="_blank"`, and the delegated handlers (`ChatPanel`, `PinnedFilePanel`, `FileViewerModal`) push the route in the same window, so Back and an unsent draft survive. A plain click on a task address (`/tasks/<id>`) from a chat, a pinned file, the file viewer or the delegated-chat note opens that task's full editor over the current view through `ItemPeekHost.vue` and `stores/itemPeek.ts`, with no URL change and one history entry at the same address (`useOverlayBackEntry`), so Back closes it through the same save; on `/tasks` routes, and for a task already open on the board, it pushes as before. The overlay is the board's own editor: a close, a route change or a workspace switch awaits `requestClose()`, so a blank title, a refused or incomplete save, or a busy nested sheet cancels the action and leaves the draft on screen. `parseTaskHref` and `isPlainLeftClick` in `lib/appLinks.ts` are the shared parse and modifier check. Modified clicks and the middle button stay the browser's (new tab/window), and a dirty dialog's refused close cancels the navigation. Every other href — an unknown path, an extra query, a different hash, `/api`, `/ws`, a protocol-relative or external URL — stays an ordinary link in a new tab. File links are separate: opening a file still goes through `data-file-path` to the viewer, not through a page address.
- **The Memory page has four sections, and no review queue.** `/memory/:section` is `map`, `categories`, `unsure` or `care` (`MEMORY_SECTIONS` in `stores/memoryMap.ts`; bare `/memory` replaces itself with the last one visited, the map on a first visit). The agent writes memory directly and Git is the record and the undo, so there is nothing to accept or dismiss: what it is unsure of is one line each in the workspace's `Workspace/Unsure.md`. `MemoryUnsurePanel.vue` reads that file through `GET /api/workspace-file?exact=1&path=<vault_root>/Workspace/Unsure.md` (a 404 is an empty list, not an error), lists the unticked lines in file order through `lib/unsureList.ts`, and offers **Open Unsure.md** (the file viewer) and **Go through it with Ciaobot** (a file discussion). Ticking or deleting a line is an edit to that file, made by the reader or in chat; the page has no write path and the sidebar row carries no count, because a count would be a file read on every page. A skill or guide suggestion is a file in `Workspace/Proposals/` that the agent writes and `ciao proposal-check` validates; the PWA has no surface for those. Removed with the queue (and with the conversation import pipeline, replaced by the `import-conversations` stock skill; a stored `/memory/import` address is an unknown section): `/proposals`, `/memory/review`, `/memory/retired` and `/memory/history` (no redirects), the Memory handover and per-kind acceptance settings card, the onboarding memory choice (the Access step keeps its id `access_memory` because stored onboarding receipts name it), and the Usage page's memory pipeline and Memories saved figures.
- **Workspace care is a read of one file.** `stores/careReport.ts` reads `GET /api/care-report?workspace=` (the engine's parse of `Workspace/Care-Report.md`; `running` is the care schedule's own `last_status`) for the active workspace, and three surfaces share it: the care row in Home's *Needs you* (`HousekeepingStrip.vue`, which joins the read to its first-load gate once), the line in *At a glance* (`HomeReviewSummary.vue`) and `MemoryCarePanel.vue` at `/memory/care`. Home shows the signal once: `composables/useHomeRail.ts` reads the resolved column count of `.home-workbench` (ChatLayout wires it with a function ref and a ResizeObserver), so it follows the same `chat-pane` container query that docks or drops the rail, with no width constant of its own. Docked, only the At a glance line shows (an actionable button, warning treatment when care needs the owner) and the Needs you row is not built; dropped, only the row shows and the rail draws no line. Hide for now hides the row only. All wording is in `lib/careReport.ts` (pure, tested per state against `lib/careReportFixture.ts`, which the stories and the e2e spec reuse). The report is re-read when the care schedule's row changes and, while a run is in progress and a surface is mounted, every 15 s. **Run care again** is `POST /api/schedule-run/<schedule.id>`; **Fix with a chat** is `startFileDiscussion` (a draft, never sent). Nothing here writes the report.
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
  **Needs-you actions are engine-owned and transient** (`ChatInfo.actions`, `chat_actions_changed`, the snapshot's `chat_actions`, kept in `stores/projects.ts`'s `chatActions` under the same read-start watermark and clock as the chat reads; `startChatAction`/`dismissChatAction`/`continueChatAction`; a 409 adopts the engine's list). ChatPanel renders every action in the bounded keyed `NeedsYouStack.vue`, and any held action counts as attention along with failed turns, questions, permission and task blockers. When a card appears or changes state the stack scrolls itself (never the page, never the focus) to that card's `data-needs-you-primary` element, else its `.needs-you-btn--primary`; the device code carries the marker. `NeedsYouCard.vue` owns Not now and Continue, a per-kind body in `components/needs-you/` owns the primary control, and `lib/chatActions.ts` owns the copy (`actionCopy` returns null for a kind with no UI yet, and such actions are not shown).

  **Running background shells are engine-owned too** (`chat_shells` and the snapshot's sparse, complete `running_shells` map, kept in `stores/projects.ts`'s `runningShells`, read with `chatRunningShells`). It is the one per-chat source: `ChatSignals.vue` draws the run glyph from it, a collapsed project in the sidebar adds the run glyph when one of its chats runs shells, Home's Working tier uses `chatIsRunning` (streaming or shells), and `runningWorkFor` drops the composer's shell rows when it is 0. Attention (`chatFailed`, needs-you, notification counts) and the title rails still read `chatIsWorking`, which is streaming only.

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
  `SettingsWorkspaceOverview.vue` draws the Workspace, Claude context trimming and
  Chats and models cards (one editor, one Save changes /
  Discard row, in the last form card, Chats and models), then Shared brains, Update
  task history and, for an archivable workspace, Archive this workspace. The
  editor state is not in the component: `composables/useWorkspaceAdmin.ts` owns
  the registry forms, drafts, save, archive and restore, `SettingsView.vue` calls
  it once and `provide`s it (`WORKSPACE_ADMIN_KEY`), and the Overview `inject`s
  it, so the Overview cards and the All workspaces card share one load. Overview
  also renders `SettingsAllWorkspaces.vue`, the list of every workspace. A workspace switch
  drops the previous draft; one started from the UI while the draft is dirty
  asks first through `projectStore.setWorkspaceSwitchGuard`.
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
  so it follows that order. The MCP tab is one button per installed agent
  (`ProviderMcpButton.vue`). The button is labelled "Open MCP settings in {cli}"
  and starts that agent's CLI (`mcp_command`) in the in-app terminal via
  `composables/useRunInTerminal.ts`, then types `mcp_slash` (`/mcp`, or `/mcps` for OpenCode) once the CLI
  has drawn and gone quiet; the follow-up is never sent before the first output,
  and it is abandoned when the session ends or the wait reaches 8 s with no output.
  It falls back to a copy of the launch command when the terminal is not ready. *Ask Ciao to add one* still starts a chat titled "Add an MCP server".
  Changes apply to new chats; a running chat loads them after `ciao chat reload`.
  The page does not read MCP servers or turn them on or off. The Providers tab instead shows generic sign-in commands for the user to copy or type manually, and opens the selected workspace's browser terminal without sending input. Verify rereads provider and model information; OpenCode configuration is not credential verification and completed model reads do not prove catalog availability. While Verify or logout is pending, and after an ambiguous result, that provider's choices and default are removed from model pickers and an uncertainty message remains until a later provider action and model refresh complete; other providers stay available. A canceled logout confirmation changes nothing.
  Chat details has an MCP section that holds the same `ProviderMcpButton.vue`
  for the chat's own agent. ChatPanel reads `/api/settings/providers` once, the
  first time the details rail or drawer opens, and keeps the row for that pane;
  a failed read shows a one-line alert. There is no health observer, no
  connection marker on the details trigger or the ⋯ menu, and no sign-in card:
  signing in happens in the agent's own MCP command. The standalone trimmed,
  case-insensitive `/mcp` opens Chat details on the MCP heading and sends
  nothing; when the agent's row has no `mcp_command` it shows why instead.
  Arguments are ordinary messages. Opening clears only text, preserving all
  four staged arrays, history, queue, offline state and uploads. While
  streaming Stop remains primary with a separate Open action; IME, picker,
  touch and pane guards still apply.

  Shared settings styling lives in `components/settings/settingsPanels.css`,
  loaded by both sides with `<style scoped src>` — a parent's scoped rules
  never reach a child, and the alternative is silently unstyled markup. New
  panels split out of Settings reuse that sheet rather than copying rules.
  Asset origin badges come from `lib/assetOrigin.ts`, which stays Vue-free.
- Completed chat traces stay collapsed as one compact `Activity` row. A settled active-session turn can hold an earlier explicitly completed reply before a hidden continuation; the server tags that row `phase: 'final_answer'` and the reader renders it as its own bubble, with compact `Activity` segments between it and the closing reply. The closing answer alone keeps the turn footer and `Outputs`. Touched-file chips sit below the final answer under `Outputs` (including files created via `Write` or common Bash redirects/`touch`/`cp`); interrupted turns keep their file chips inside `Activity` so unfinished work remains visible. Newly created files are labelled `new` on the chip.
- Conversation forks are initiated from the final assistant reply action group (Copy/Fork). The PWA sends the selected message slice up to that reply and redirects to the newly created chat, focusing the composer.
- New PWA actions (state-changing routes) must be documented in `../PWA_API.md` → Agent recipes, or whitelisted in `../tests/test_pwa_api_docs.py`.
- **The embedded terminal screen** (`lib/terminalRenderer.ts`) is a Vue-free primitive that owns one physical xterm bound to one session. The rules it keeps, and why: the session id is fixed at construction and there is no way to rebind it, because a screen that showed two sessions would be two histories at once; xterm's options object never leaves the module, so input is switched only by `setInputEnabled(boolean)` and starts off — a screen nobody has claimed sends nothing; a write is acknowledged only from xterm's own write callback, and the number it carries is the absolute end offset of the bytes just parsed, so a resume can use it and nothing else; a write that is still queued or in flight when the screen is disposed is **rejected, never resolved**, so a disposal never advances an acknowledged offset and the bytes come back on the next replay; the theme is read from the app's own tokens and resolved to the `rgb()`/`rgba()` values xterm can parse, and a `color-mix()` token is refused rather than passed on; one ResizeObserver, owned by the renderer, measures the screen and is disconnected on dispose. A link in the output (an OSC 8 hyperlink, or a plain `http(s)://` URL found by `@xterm/addon-web-links`) opens in a new tab with `noopener,noreferrer`, in the browser the user is on. Every other scheme is refused, and hovering a link names its host in the screen's tooltip (`Open <host>`). xterm and its fit and web-links addons are imported dynamically, so none of them enters the bundle of a page that shows no terminal — Home's payload is unchanged by the primitive existing. The pane that shows a screen is not in this repository yet.

- **Chat loops are a view of one chat's own automations.** A loop is an interval schedule with `owner_chat_id` (set only by the engine's loop creator); `lib/loops.ts` is the pure helper set (`isOwnedLoop`, `loopsForChat`, `loopRunsLeft`, `loopEndsLabel`, `loopSummary`) and tolerates a row from an engine without the loop fields. `lib/scheduleList.ts` gives loops their own **Loops** group between One-offs and Routines (sidebar, title rail and keyboard roaming read the same order). The overview shows them in their own section, out of Next up and Paused; the detail page hides Edit on When, Where it runs and Model (lifetime and target are fixed at creation), hides Run now, the pause toggle and the ⋯ menu, and offers one **Stop loop** (confirmed). `components/ChatLoopChip.vue` is the composer-bar chip for the chat's own loops: its menu opens each loop in Automations or stops it. Stop is the existing `DELETE /api/schedules/{id}` through `taskStore.deleteSchedule`, with no confirm (the menu is already the second tap); a 404 means the loop already ended and counts as stopped, any other failure reads inline and keeps the menu open. Times are absolute (`ends 14:30`, with the date when it is not today), there is no countdown or timer, and loops are never paused. A loop that fails is kept in the inbox like any automation.
- **The notification inbox is a derived view, not a source of truth.** The inbox, opened from the bell in `components/NotificationBell.vue` (body in `components/NotificationInboxList.vue`), projects three sources, keyed `chats`, `operator` and `automations`. `chats` is the `projects` chat predicates plus `taskSignals` board review, and carries board-result rows; a memory pass never contributes a row, blocked or not, because its lifecycle stays on Home's memory-insight rows; `operator` is `housekeeping.actions`, package and provider updates included, with `github-star` excluded; `automations` is `tasks.schedules` through `scheduleInWorkspace` and `scheduleNeedsAttention`. `stores/notificationInbox.ts` only adapts those owners and tracks health, and `lib/notificationInbox.ts` is the pure projection. The count is distinct rows, not messages: a chat that needs the user and is unread is one *Needs you* row with an unread marker, a board result owns its review row, and an automation incident suppresses its linked failed chat. `99+` is visual only; the accessible name carries the full number. Reading is not resolving: opening the inbox marks nothing read, opening a chat clears unread through the existing `chat_read` lifecycle, and a pending question or permission stays a row. There is no dismiss, snooze or mark-all-read; resolve at the source. Exact links go to `/chat/<id>`, `/tasks/<id>`, `/schedules/<id>`, `/settings` or `/settings/providers`, and to `/?notice=<key>` for other operator notices. A chat, task or automation whose target has gone re-reads, shows *That item is no longer available* and navigates nowhere. Freshness is per source (`checking`, `ready`, `stale`, `unavailable`); the worst one is the overall status, and an empty list is all clear only when every source is `ready`. Retry calls the owners' read methods only. `init()` runs from `ChatLayout` after bootstrap and `dispose()` on unmount; it installs one focus/visibility listener with a 5-second throttle and a scoped set of watchers, has no timer, and rejects late work with a generation token after a workspace switch. A new source gets a key in `INBOX_SOURCES` and a builder in the store's snapshot table, is seeded in every all-clear test fixture, and never gets a mutation path.

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
| `shared-brain-settings.spec.ts` | The Shared brains card on Settings › Workspace › Overview at 390px: the Link type selector is usable, the path field is 16px and controls are 44px under touch, mixed Folder and Git rows keep their kinds after reload and saves, local folder availability stays distinct from cloud sync status, a refused path shows the engine's words inline, and Remove asks first and runs from the keyboard. |

| `project-decisions.spec.ts` | Project-side decision entry at 390px, with isolated permissions and save responses: shows the standing disclosure and records the decision for the owning project. |
| `sidebar-overflow.spec.ts` | That the sidebar's chat list never scrolls sideways, with project rows idle or hovered. |
| `narrow-viewport.spec.ts` | Layout at 390px. jsdom has no layout engine: every rect is 0x0 and `scrollWidth` is always 0, so neither the unbreakable-flex-child trap nor a tap target under `--touch: 44px` is visible from a mount. The memory-insight journey also measures the row that carries a second control (the retry beside the open control), which is the one place a row can grow past the pane. Selecting a reply measures the opposite case: the action footer is not in the layout until the message is selected, and selection must leave the transcript's `scrollTop` alone; the fixture's opt-in `comment` shape then measures that the selected card's accent outline does not bleed across the turn gap onto the comment-reference card below it. Structured question cases measure the always-visible, hit-testable answer footer and usable scroll region at portrait, landscape and enlarged text, and check composer/keyboard focus. |
| `browser-zoom.spec.ts` | Reflow under page zoom and at the largest in-app font scale, and that the viewport meta never disables pinch zoom. |
| `task-board-lanes.spec.ts` | That every board lane is 310px wide at 1440, 1100, 900 and iPhone widths (computed flex included), that a review card's state word and action share one line, and that Done sits collapsed behind a 44px tab that expands, hides, persists across a reload, scrolls the Done lane into view and focuses its Hide button when the tab opens it, and accepts a dropped card (opening the completion sheet). The status filter still lists done tasks at full width. Widths and line boxes only exist in a real layout engine. |
| `task-card-alignment.spec.ts` | That every text block under a task card's title (meta line, agent note, archived caption, the band's state word) starts at the title's text column, and the band's dot centres under the Done ring, at 1280px and under iPhone touch emulation where the checkbox box is 44px instead of 28px. The hanging indent leans on both netting 20px, which only layout can confirm. |
| `webhook-actions.spec.ts` | Every trigger control and its delete menu stays within the viewport and passes corner hit-tests at desktop, 390px touch width and 200% page-zoom reflow, with History closed and open. Ancestor clipping can hide controls without causing document overflow. |
| `appearance-settings.spec.ts` | Settings › Appearance is engine-wide. Body font swaps the prose face while code stays monospace. Soft text contrast resolves `--fg` to `#d2d2de` / `#2e2f46` in dark and light, text contrast and theme survive a reload with the local caches cleared (only the engine can restore them), a refused custom pair keeps Apply disabled and says why while a passing one applies live and Reset clears it, and under Pixel 5 touch emulation every control in the card and in the Custom colours dialog is at least 44×44px with no sideways scroll at 390px. |
| `touch-targets.spec.ts` | Home, chat, tasks, automations, every Memory section, the project, schedule and task detail pages and every Settings tab under iPhone touch emulation: every control at least 44×44px. It also clicks each menu, popover and dialog trigger and measures what opens (model pickers, the workspace form, chat details), and checks `.field-info` panels stay inside the viewport and beside their glyph. Measures buttons, links, ARIA widgets, inputs (checkboxes and radios through their label), `summary` and opened `<details>`; inline prose links and visually hidden keyboard controls are exempt. jsdom applies no CSS, so only a real engine under `(pointer: coarse)` can measure it. |
| `events-reconnect.spec.ts` | That the *browser* notices a severed `/ws/events` socket, re-dials, and applies the snapshot the new socket carries. A vitest fake can only close itself. |
| `chat-shells-mark.spec.ts` | That a chat that is not open shows its running background shells on its sidebar row and its Home row (run glyph, *N shells running*, Home's Working tier) from `chat_shells` frames, and that a reconnect snapshot without the chat (an engine restart) clears both. |
| `archived-chat.spec.ts` | That an archived chat opens read-only from a deep link: no composer, and no chat socket opened for a session the provider has already reclaimed. And that a project's archived list opens the transcript in the file viewer, with its "Continue this chat", instead of the chat's pinned side tile: the split view exists only in a real layout, so jsdom cannot see the tile the project route must not have. |
| `workbench-layout.spec.ts` | That Home's review rail sits beside the command surface, that the expanded sidebar stacks workspace scope, New chat and the destinations without overlap, and that every boundary between Home's sections is the same gap token at desktop and at 390px (a per-section `margin-top` under a shared `gap` reads as a doubled gap, which no unit test can see). |
| `chat-loading-layout.spec.ts` | That the held history-loading skeleton has separated rows within the chat pane at desktop and phone widths. |
| `reply-not-folded.spec.ts` | That a short closing reply followed by a reasoning-only step renders as a bubble after a phase-less history replay (OpenCode shape) instead of being folded into the collapsed Activity trace (#630). The fold is a render heuristic, so only a real render shows it. |
| `terminal-renderer.spec.ts` | That the terminal screen really draws. The bytes are written to a real xterm, a keystroke reaches the shell only while input is enabled, a resize changes the window, and a disposed screen leaves nothing behind — no DOM, no listener, no console output. jsdom has no xterm, no layout and no font metrics, so the screen itself can only be judged in a browser. The spec drives the real renderer through a test-only page (`e2e/fixture/harness/`), built into its own tree by `npm run build:harness` and served by the fixture under `/__fixture__/harness/`; the production build writes to a different directory, so the PWA that ships can never carry it. |
| `notification-inbox.spec.ts` | What a real browser decides for the inbox: the bell opens the inbox popover and Escape closes it without leaving the page; a second device's server-side `chat_read` converges over a real socket, and opening the inbox writes nothing; partial failure and stale rows are never all clear; a request still in flight across an A, B, A workspace round trip is rejected; an exact Home notice is revealed without running; a removed target shows *That item is no longer available* and stays; the collapsed-rail bell shows its count and partial ring; at 390px the pane-header bell opens the sheet and following a row closes it; and at 640px with a long hostile title nothing scrolls sideways, every link and button stays in the viewport, and reduced motion stops the inbox's animations. |

The fixture serves an empty chat history by default. A spec that needs real
turns to select opts in per session with `POST /__fixture__/transcript`, so the
specs beside it keep seeing the empty chat. `POST /__fixture__/needs-you` is the same opt-in for
the housekeeping rows of the "Needs you" list, which is empty by default because the Home gap
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
job runs on release PRs into `main`; because it has already built the PWA for
its own build check, it runs `npm run build:harness` and `npx playwright test`
directly instead of paying for that prehook's second PWA build.

### Still manual

Browser automation does not reproduce iOS PWA suspension, the software
keyboard, or native-shell behaviour. The device checklist in "iOS PWA gotchas"
above still has to be walked by hand on a real phone before a release.

Onboarding's care stage (`components/onboarding/OnboardingCareStage.vue`) asks when the host is usually available, then lets the operator edit the installed workspace-care routine's local time and IANA timezone. The fields propose 07:00 in the browser's timezone (a routine already in a zone other than the packaged UTC is proposed as it is), and Continue commits what they show to that existing row through the schedules API via the stage's exposed `save()`; fields that already match the engine write nothing. No routine is created when the workspace has none.


### Onboarding stages

The bootstrap login setup offers an optional separate existing notes folder. It inspects the location on the host and requires a successful inspection before submitting the resolved location to setup; synchronization remains the cloud client's responsibility.

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

### Onboarding entry

`/onboarding` composes the stage components by the receipt's recorded step and advances progress only
through the onboarding store; `OnboardingView` never writes progress itself, and the access
screen advances only when its stage reports ready. `HomeOnboardingResume` reads the current
receipt on Home entry and window focus and links into the journey while a target is pending — it is
not a dismissal, and it shows nothing when the receipt cannot be read.
