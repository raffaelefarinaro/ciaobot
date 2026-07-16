# PWA frontend

Vue 3 + Vite + Pinia + TypeScript. Built output goes to `ciao/web/static/`, served by the same Starlette server as the API. See `../README.md` for the repo-wide layout and `../PWA_API.md` for backend routes.

The file viewer is Vue-first, with one intentional React bridge: `ExcalidrawViewer.vue` mounts `@excalidraw/excalidraw` through `react-dom/client` so `.excalidraw` JSON files can render as read-only diagrams in the Preview tab. History and Diff still operate on the raw JSON snapshots.

## Dev workflow

```bash
cd web
npm install          # first-time only
npm run dev          # local Vite dev server (proxies API to localhost)
npm run build        # type-check + production build into ../ciao/web/static/
npm test             # vitest
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
    router.ts             routes: /login, /, /chat/:id, /project/:id, /schedules, /settings, /settings/:tab
    components/           one Vue SFC per feature pane (e.g. SubagentPanel.vue, ProviderSubchatPanel.vue for active/historical sub-chats)
    stores/               Pinia stores (auth, projects, tasks, fileViewer)
    lib/                  pure helpers (api, time, safeMarkdown, etc.)
```

## iOS PWA gotchas

The PWA runs primarily as a standalone iOS Safari app. Several iOS-specific quirks are addressed in code; do not undo them without reading why.

### Keyboard + viewport

- Viewport meta in `index.html` carries `interactive-widget=resizes-content` on purpose. With that flag, when the iOS keyboard opens the layout viewport shrinks alongside the visual viewport. The keyboard-open detection in `main.ts` relies on this: it tracks the tallest viewport height seen per orientation and toggles `html.keyboard-open` when the current `visualViewport.height` drops below ~85% of that max.
- `--app-h` is set off `window.visualViewport.height` (falling back to `innerHeight`), so the chat layout snaps instantly when the keyboard opens or closes. Plain `100dvh` is not enough on iOS Safari; it does not update until the user interacts with the page.
- `visualViewport` `scroll` events are intentionally NOT listened to. iOS fires them while the page shifts to keep the caret visible during multi-line typing, and re-reading `vv.height` there can latch a stale/smaller value, collapsing the messages area.
- `window.scrollY` is force-clamped to 0 in `main.ts`. iOS can still shift the document when the keyboard opens, leaving the input bar floating with a gap below it.
- `html.keyboard-open` collapses `--safe-bottom` to 0 in `App.vue`. Without that, the home-indicator safe-area inset adds dead space below the input bar while the keyboard covers the home indicator.

### Zoom and text scaling

Browser pinch zoom remains enabled for accessibility in both Safari tabs and
standalone PWA mode. Do not add `user-scalable=no`, `maximum-scale=1`, or
WebKit gesture-event blockers. Individual controls may use
`touch-action: manipulation` to avoid delayed/double activation without
disabling page zoom. The in-app font scale under Settings > Appearance is an
additional convenience, not a replacement for browser zoom.

### WebSocket suspension

iOS Safari suspends JS and WebSockets when the PWA is backgrounded. On resume, `readyState` may still report `OPEN` while no events flow. Listen for `visibilitychange` (visible) and `pageshow` (bfcache restore) on any view that depends on a WebSocket, and force-disconnect + reconnect.

### Layout traps

- **Do not use `scrollIntoView` on nested scrollable containers.** iOS Safari can scroll the wrong ancestor. Compute `offsetTop` relative to the scroll container and call `scrollTo({ top, behavior: 'smooth' })` directly. See `scrollToHighlight` / `scrollSidebarToCard` in `ChatPanel.vue`.
- **Flex children with unbreakable content need `min-width: 0`.** Without it, a long unbreakable string (a URL, a model identifier, etc.) forces the flex parent wider than the viewport and breaks horizontal layout.
- **Tap targets** must hit the `--touch: 44px` minimum (declared in `App.vue`). Icon-only buttons use the `.btn-icon` utility which enforces this. Visually small actions can wrap a 44px hit area around an 18px glyph instead of resizing the glyph.

## Design system

CSS custom properties live in `App.vue` as `:root` declarations. The system is opinionated:

- **Color**: Deep blue-violet surfaces (`--bg #1a1a2e`, `--bg2 #1f2240`, `--bg3 #2a2e54`, `--bg-elev #23264a`), pink accent (`--accent #ff4d6d`, `--accent-strong #ff2e54`), violet secondary (`--accent2 #6a47b8`). A clean light theme is supported via `.theme-light` overrides.
- **Type**: Monospace stack (SF Mono, Fira Code, Cascadia Code). Scale: 11/12/13/15px (`--text-xs`, `--text-sm`, `--text-base`, `--text-lg`), dynamically adjusted via the client-side `--font-scale` multiplier (from 0.8x to 1.5x, configured under Settings > Appearance).
- **Geometry**: 10/6/14px radii (`--radius`, `--radius-sm`, `--radius-lg`). Spacing scale `--space-1` through `--space-6`.
- **Motion**: `--ease: cubic-bezier(0.2, 0.8, 0.2, 1)`.
- **Wordmark**: `.wordmark` (with size modifier `--lg|--md|--sm`) renders `› word` with a pink chevron prefix. Used in StartupView, LoginView, ProjectSidebar brand, and empty states.
- **Caret**: `.caret` is a blinking pink terminal caret. Pair it with the wordmark for "live" surfaces (login prompt, idle empty state).
- **Body**: carries a 2.5% SVG noise overlay via `body::before` for subtle CRT grain.

Shared utility classes (defined globally in `App.vue`): `.btn-primary`, `.btn-small`, `.btn-icon`, `.btn-chip`, `.badge` (with `--accent|--accent2|--muted|--success|--warn|--error|--dot`), `.page`, `.card`, `.form-grid`, `.form-group`, `.form-actions`, `.hint`, `.checkbox-pill`, `.modal-backdrop`, `.modal-sheet`, `.sr-only`.

Prefer the utility classes over re-inventing the same button/badge/card per component.

## Conventions

- One Vue SFC per pane. Keep `<script setup lang="ts">`, template, scoped `<style>`.
- Markdown rendering goes through `lib/safeMarkdown.ts` (DOMPurify + marked + highlight.js). Never `v-html` raw user content.
- DOM manipulation that needs to bypass Vue's scoped attribute (e.g. inline highlight spans inserted into rendered markdown) uses `:deep(...)` in the scoped stylesheet.
- Completed chat traces stay collapsed as one compact `Activity` row. Touched-file chips sit below the final answer under `Outputs`; interrupted turns keep their file chips inside `Activity` so unfinished work remains visible.
- Conversation forks are initiated from the final assistant reply action group (Copy/Read aloud/Fork). The PWA sends the selected message slice up to that reply and redirects to the newly created chat, focusing the composer.
- New PWA actions (state-changing routes) must be documented in `../PWA_API.md` → Agent recipes, or whitelisted in `../tests/test_pwa_api_docs.py`.

## Testing

- `npm test` runs vitest.
- Mount smoke test: `src/components/__tests__/mountSmoke.test.ts` mounts every top-level pane to catch template / setup errors.
- Pure-function tests live next to the source (`lib/safeMarkdown.test.ts` pattern).
- No e2e suite yet. UI changes are verified by deploying and running the PWA.
