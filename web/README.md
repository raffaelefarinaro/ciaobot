# PWA frontend

Vue 3 + Vite + Pinia + TypeScript. Built output goes to `ciao/web/static/`, served by the same Starlette server as the API. See `../README.md` for the repo-wide layout and `../PWA_API.md` for backend routes.

The file viewer is Vue-first. Text, Markdown, CSV, PDF/PPTX (via a binary preview), and HTML artifacts all render through native Vue components (`HtmlArtifactViewer.vue`, `CsvViewer.vue`, plus text/iframe previews). Diagrams live inside HTML artifacts as inline SVG; `.excalidraw` is no longer a viewer type. History and Diff operate on the raw file snapshots.

## Dev workflow

```bash
cd web
npm install          # first-time only
npm run dev          # local Vite dev server (proxies API to localhost)
npm run build        # type-check + production build into ../ciao/web/static/
npm test             # vitest
npm run test:e2e     # Playwright browser suite (see Testing below)
```

Type-checking runs via `vue-tsc --noEmit` as part of `npm run build`. PR-blocking minimum: `npm run build` passes.

After changes that touch running static assets (anything that lands in `ciao/web/static/`), redeploy via the PWA's Settings → Deploy. **Do not restart the ciao service yourself**: you'd sever the PWA session the agent is talking through.

## Layout

```
web/
  index.html              entry HTML, viewport meta, PWA manifest link
  src/
    main.ts               Vue bootstrap + iOS viewport / keyboard / zoom plumbing
    App.vue               root component, global CSS tokens (--bg, --fg, --accent), wordmark + caret + noise overlay
    router.ts             routes: /login, /device, /, /chat/:id, /project/:id, /schedules, /memory, /settings, /settings/:tab
                          (/device is device-scoped and unguarded: it must load when a client's host is down)
    components/           one Vue SFC per feature pane (including CommandPaletteModal.vue and FileViewerModal.vue)
    stores/               Pinia stores (auth, projects, tasks, fileViewer)
    composables/          reactive logic shared between components, and behaviour lifted
                          out of oversized panes (useHoverPinPopover, useChatComposer)
    lib/                  pure helpers (api, time, safeMarkdown, etc.) — no Vue imports
```

## iOS PWA gotchas

The PWA runs primarily as a standalone iOS Safari app. Several iOS-specific quirks are addressed in code; do not undo them without reading why.

### Keyboard + viewport

- All of this lives in `lib/viewport.ts` (`installViewportPlumbing()`, called once from `main.ts`, idempotent) and is pinned by `lib/viewport.test.ts`. Add a test there for any change to the measurement rules. The module also exports `viewportHeight()` / `viewportWidth()` (the size a `position: fixed` element should measure against) and `onViewportChange()`, wrapped for components by `composables/useViewportHeight.ts`.
- Viewport meta in `index.html` carries `interactive-widget=resizes-content` on purpose. With that flag, when the iOS keyboard opens the layout viewport shrinks alongside the visual viewport. The keyboard-open detection relies on this: it tracks the tallest viewport height seen per orientation and toggles `html.keyboard-open` when the current `visualViewport.height` drops below ~85% of that max.
- `--app-h` is set off `window.visualViewport.height` (falling back to `innerHeight`), so the chat layout snaps instantly when the keyboard opens or closes. Plain `100dvh` is not enough on iOS Safari; it does not update until the user interacts with the page.
- `visualViewport` `scroll` events are intentionally NOT listened to. iOS fires them while the page shifts to keep the caret visible during multi-line typing, and re-reading `vv.height` there can latch a stale/smaller value, collapsing the messages area.
- The viewport is re-measured on `visibilitychange` (visible) and `pageshow`, each time as a short burst (0/50/200/500ms) rather than a single read. iOS suspends JS while the PWA is backgrounded, and if the keyboard was open it gets dismissed during the suspension with no `resize` ever reaching the page, so `--app-h` stays latched at the keyboard-open height and the layout keeps a dead zone under the input bar. The burst exists because `vv.height` still reports the stale value at the instant of resume. The resume path deliberately does NOT reset the tallest-seen height: if the keyboard is genuinely still up at resume, that would record the shrunken height as the maximum and keyboard-open would never be detected again.
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

### Layout traps

- **Do not use `scrollIntoView` on nested scrollable containers.** iOS Safari can scroll the wrong ancestor. Compute `offsetTop` relative to the scroll container and call `scrollTo({ top, behavior: 'smooth' })` directly. See `scrollToHighlight` / `scrollSidebarToCard` in `ChatPanel.vue`.
- **Flex children with unbreakable content need `min-width: 0`.** Without it, a long unbreakable string (a URL, a model identifier, etc.) forces the flex parent wider than the viewport and breaks horizontal layout.
- **Tap targets** must hit the `--touch: 44px` minimum (declared in `App.vue`). Icon-only buttons use the `.btn-icon` utility which enforces this. Visually small actions can wrap a 44px hit area around an 18px glyph instead of resizing the glyph.
- **`position: fixed` popovers clamp through `lib/popoverAnchor.ts`**, against the *visual* viewport (`lib/viewport.ts`), not `window.innerHeight`. Anything a fixed popover pushes off screen is unreachable, because scrolling does not move it. A popover that focuses an input must also clamp *reactively*, via `useViewportHeight()`: the keyboard opens a moment after the box is placed, and a one-time measurement leaves it stranded behind the keyboard. `CommentComposePopover.vue` is the reference.

## Design system

CSS custom properties live in `App.vue` as `:root` declarations. The system is opinionated:

- **Color**: Deep blue-violet surfaces (`--bg #1a1a2e`, `--bg2 #1f2240`, `--bg3 #2a2e54`, `--bg-elev #23264a`), pink accent (`--accent #ff4d6d`, `--accent-strong #ff2e54`), violet secondary (`--accent2 #6a47b8`). A clean light theme is supported via `.theme-light` overrides.
- **Type**: Monospace stack (SF Mono, Fira Code, Cascadia Code). Scale: 11/12/13/15px (`--text-xs`, `--text-sm`, `--text-base`, `--text-lg`), dynamically adjusted via the client-side `--font-scale` multiplier (from 0.8x to 1.5x, configured under Settings > Appearance).
- **Geometry**: 10/6/14px radii (`--radius`, `--radius-sm`, `--radius-lg`). Spacing scale `--space-1` through `--space-6`.
- **Motion**: `--ease: cubic-bezier(0.2, 0.8, 0.2, 1)`.
- **Wordmark**: `.wordmark` (with size modifier `--lg|--md|--sm`) renders `› word` with a pink chevron prefix. Used in StartupView, UpdateProgressView, LoginView, ProjectSidebar brand, and empty states.
- **Caret**: `.caret` is a blinking pink terminal caret. Pair it with the wordmark for "live" surfaces (login prompt, idle empty state).
- **Body**: carries a 2.5% SVG noise overlay via `body::before` for subtle CRT grain.

Shared utility classes (defined globally in `App.vue`): `.btn-primary`, `.btn-small`, `.btn-icon`, `.btn-chip`, `.badge` (with `--accent|--accent2|--muted|--success|--warn|--error|--dot`), `.page`, `.card`, `.form-grid`, `.form-group`, `.form-actions`, `.hint`, `.checkbox-pill`, `.modal-backdrop`, `.modal-sheet`, `.sr-only`.

Prefer the utility classes over re-inventing the same button/badge/card per component.

## Conventions

- One Vue SFC per pane. Keep `<script setup lang="ts">`, template, scoped `<style>`.
- Load states are separate states. A list that fetches (`ProposalReviewPanel`, `ProposalHistoryList`, the Memory Map) must distinguish first-load in flight, first-load failure (inline error + Retry, never an empty-state claim), a failed refresh over existing rows (keep the rows, mark them stale, offer Retry), a filter hiding a non-empty set (offer to clear filters), and a genuinely empty set. Never derive "there is nothing here" from a filtered array alone — a failed or pending GET would then read as a cleared queue. Load errors live in their own store slot (`loadError`), apart from action errors (`error`), so a list refresh cannot clear an unread accept/dismiss failure.
- Markdown rendering goes through `lib/safeMarkdown.ts` (DOMPurify + marked + highlight.js). Never `v-html` raw user content.
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
- Completed chat traces stay collapsed as one compact `Activity` row. Touched-file chips sit below the final answer under `Outputs` (including files created via `Write` or common Bash redirects/`touch`/`cp`); interrupted turns keep their file chips inside `Activity` so unfinished work remains visible. Newly created files are labelled `new` on the chip.
- Conversation forks are initiated from the final assistant reply action group (Copy/Read aloud/Fork). The PWA sends the selected message slice up to that reply and redirects to the newly created chat, focusing the composer.
- New PWA actions (state-changing routes) must be documented in `../PWA_API.md` → Agent recipes, or whitelisted in `../tests/test_pwa_api_docs.py`.

## Testing

- `npm test` runs vitest. This is where almost every test belongs.
- Mount smoke test: `src/components/__tests__/mountSmoke.test.ts` mounts every top-level pane to catch template / setup errors.
- Pure-function tests live next to the source (`lib/safeMarkdown.test.ts` pattern).

### Browser suite (`npm run test:e2e`)

`e2e/` holds a deliberately small Playwright suite — four spec files, ten tests,
about three seconds — that covers only the things a jsdom mount **cannot**
establish:

| Spec | What only a real browser can decide |
| --- | --- |
| `workspace-shortcuts.spec.ts` | Where a typed character actually lands. The `1`-`9` shortcuts must follow the visible sidebar order and stay inert while a text field is focused; jsdom reports a focused textarea that no keystroke is routed to. Also walks Tab through the primary nav, which is how a click-only control gets caught. |
| `narrow-viewport.spec.ts` | Layout at 390px. jsdom has no layout engine: every rect is 0x0 and `scrollWidth` is always 0, so neither the unbreakable-flex-child trap nor a tap target under `--touch: 44px` is visible from a mount. |
| `browser-zoom.spec.ts` | Reflow under page zoom and at the largest in-app font scale, and that the viewport meta never disables pinch zoom. |
| `events-reconnect.spec.ts` | That the *browser* notices a severed `/ws/events` socket, re-dials, and applies the snapshot the new socket carries. A vitest fake can only close itself. |

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
