# Page loading performance plan

## Installed Home data wait — 2026-10-11

After the asset improvement was installed, the real Home bootstrap still waited
on `/api/chats`. An authenticated individual request took 2.327 s. A reload
showed the Home skeleton disappearing 4.822 s after the timing observer started;
requests for projects, workspaces and vault review were delayed alongside chats.
These local browser measurements use the real catalog, with no network throttling,
and are separate from the synthetic asset-download results below.

The read-only profile of 2,324 chat rows found about 513,000 filesystem stats:
each missing Claude session repeated a cross-cwd lookup across all project slug
directories. The fix checks preferred session paths as before, then shares one
fresh directory scan across the remaining requested UUIDs. Individual session
lookup contracts and all returned availability flags are preserved. No file
presence cache is retained between requests.

Three alternating old/new comparisons on the same catalog produced byte-for-byte
equivalent JSON data: old listing computation 2,339 / 2,319 / 2,331 ms, new
44 / 42 / 41 ms. This measures backend computation, not the final browser
indicator duration; live activation and browser verification follow separately.

Baseline measured on 2026-10-11 against commit `9fcecf92`; implementation
verified in the working tree on the same investigation date.

Cold asset downloads dominate Home loading in the synthetic browser experiments.
The implementation compresses hashed JS/CSS, loads chat and file viewers on demand,
and prefetches chat code for deep links. Startup progress shares a ten-second
request deadline with engine monitoring and waits 180 ms before displaying its
curtain. Large-catalog work remains a separate, conditional investigation.
The installed origin's encoding and real-device waterfall still need measurement.

## Implemented result

A Luna subagent implemented the approved scope, followed by independent code
review and production-build verification. The final comparison used five runs
per condition with the same mobile throttling as the prototypes. Implemented
asset requests passed through the actual `create_app` static route and gzip
middleware served by Uvicorn; invented APIs and WebSockets stayed in the Node
fixture. The original build was preserved before implementation.

| Condition | Original median | Implemented median | Implemented range |
|---|---:|---:|---:|
| Cold Home | 6.841 s, identity assets | 2.501 s | 2.495–2.516 s |
| Cold chat deep link | 2.900 s, gzip assets | 2.854 s | 2.848–2.864 s |
| Warm Home with HTTP asset cache | 0.797 s, gzip assets | 0.762 s | 0.762–0.763 s |

Cold Home readiness improved by 63.4%. Completed Home JS/CSS encoded bytes fell
from 1,096,605 to 229,474; decoded bytes fell to 752,179. This meets the
compressed Home budget and the proposed 2.6 s synthetic readiness budget. Chat
deep links meet the no-more-than-100-ms regression budget against compression
alone. Warm transfer remained 13,286 bytes, so the small warm improvement reflects
browser/startup work rather than another download saving. The ready-within-180-ms
no-curtain behavior is separately covered by unit tests; the throttled warm runs
do not establish a large startup-display saving.

The reviewed implementation preserves immutable caching, MIME and security
headers, zero-quality gzip rejection, weak compressed validators, conditional
responses, and identity range responses. Compression excludes API streams and
unhashed files. The first file-viewer mount initializes its watchers, subsequent
opens retain the mounted modal, and closing restores focus to the current opener.
Startup display timing does not create an extra polling loop; in-flight status
requests are shared, abort after ten seconds, and ignore stale startup callbacks.
Authentication recovery remains owned by the engine monitor.

Verification passed: `mypy ciao` (204 files), full backend tests (8,329 passed,
51 skipped), full frontend tests (2,568 passed), production build, and all 77
Chromium browser tests. Lint reported zero errors and five existing warnings.
Browser coverage includes optional-chunk requests and the byte budget, active
and archived chat links, first/repeated file opening, failed pins, focus return,
workspace shortcuts, event reconnect, mobile layout, and browser zoom. Home,
Chat, and the file viewer were visually inspected on mobile and desktop. All
30 timing runs reported no page JavaScript errors, and their composers accepted
draft text. Chat readiness measures composer layout, not transcript completion
or send availability.

The production service has not been restarted or deployed. Installed-origin
encoding, real catalog scale, Safari/PWA resume, and interrupted lazy-chunk
recovery remain follow-up checks. Suggested tracking issue: **[Agent] Validate
page loading on the installed origin and iPhone PWA**.

## Baseline and prototype measurements

The production build passed `npm run build`, including Vue TypeScript checking.
Experiments used the repository's invented workspace in `web/e2e/fixture`, isolated
Chromium contexts, a 390 × 844 touch viewport, and three runs per condition.
The simulated connection used 150 ms latency, 200,000 bytes/s download
(1.6 Mbps), 93,750 bytes/s upload, and 4× CPU slowdown. Local runs had no throttling.

Home readiness means the prompt textarea exists, has visible dimensions, and the
startup overlay has left the DOM. Chat readiness uses the chat composer instead.
These are useful-content readiness checks, not proof that every interaction is
ready. The overlay's 400 ms exit transition is included. No provider was launched,
and no real workspace data was read.

| Experiment | Median readiness | Range across three runs |
|---|---:|---:|
| Local cold Home, current build | 0.475 s | 0.474–0.517 s |
| Simulated mobile cold Home, current build | 6.854 s | 6.834–6.860 s |
| Same, gzip static HTML/JS/CSS | 2.910 s | 2.908–2.922 s |
| Same, gzip plus chat/viewer splitting | 2.495 s | 2.482–2.496 s |
| Simulated mobile Home, gzip and warm HTTP asset cache | 0.817 s | 0.791–0.817 s |
| Local Home, auth response delayed by 1 s | 1.102 s | 1.093–1.114 s |
| Local Home, startup-status response delayed by 1 s | 1.458 s | 1.458–1.474 s |
| Local Home, chats response delayed by 1 s | 1.099 s | 1.079–1.103 s |
| Simulated mobile cold chat deep link, gzip | 2.912 s | 2.902–2.914 s |
| Same, gzip plus simple splitting | 3.147 s | 3.136–3.171 s |
| Same, split chat code downloaded alongside bootstrap data | 2.831 s | 2.824–2.852 s |

Compression improved cold Home readiness by 58%; compression plus splitting
improved it by 64% relative to the current uncompressed fixture. Simple splitting
also introduced a 235 ms chat deep-link regression relative to compression alone.
Starting the chat-code download when the layout mounts on a chat URL removed
that regression in a follow-up prototype, reaching 2.831 s. This prototype uses
the URL only to schedule a static-code download; normal session and data gates
remain in place.

The current entry bundle is 309.51 KB JS plus 27.92 KB CSS. Its authenticated
layout adds 540.28 KB JS plus 213.27 KB CSS. Counting the small shared chunks,
completed JS/CSS resources at Home readiness totaled 1,096,605 bytes uncompressed,
313,107 bytes with gzip, and 230,036 bytes in the split prototype with gzip.
Splitting also reduced decoded JS/CSS to 750,990 bytes, so it reduces browser
work as well as network transfer. Sizes use decimal KB.

In one representative cold run, the layout download began at 2.41 s and finished
at 6.37 s; workspace, project, and chat requests started at 6.43 s. With gzip,
those data requests started at 2.52 s; with gzip and splitting, at 2.08 s.
Initial-load long tasks were roughly 50–84 ms in these samples. This points to
downloads and request sequencing as the first targets for this fixture.

The fixture normally serves uncompressed assets with `no-store`. Cold runs disabled
the browser cache explicitly. The warm-cache variant gave hashed assets the same
immutable policy as production. Service workers were blocked in all experiments
to isolate HTTP behavior. Warm resource `encodedBodySize` still reports the cached
representation's size; actual resource transfer was about 13.3 KB.

These results exclude the installed engine, reverse proxy, real chat-catalog size,
Safari, and installed-PWA resume. A 4× CPU slowdown is a simulation, not a phone
benchmark. First paint and early LCP observations mostly described the startup
screen, so they are not used as evidence of useful-content speed. INP was not
measured. All completed experiment runs reported no page JavaScript errors.

## Baseline bottlenecks

- `ciao/web/app.py` configures security and auth middleware, but no response
  compression. Vite's gzip size column estimates compression; it does not make
  the server send compressed bytes. A deployment proxy may already compress them.
- `web/src/router.ts` waits for the auth module and session check before resolving
  the layout. The layout then loads its statically imported components before
  `fetchAll()` starts. Existing route splitting and async Settings/Memory panels
  are already useful; the remaining eager chat/viewer imports matter on Home.
- `web/src/components/ChatLayout.vue` statically imports `ChatPanel`,
  `FileViewerModal`, and `PinnedFilePanel`. The modal is mounted even when closed.
  The prototype made these async and mounted the modal only when open.
- `web/src/stores/projects.ts:fetchAll` already fetches workspaces, projects, and
  chats concurrently. It still gates Home on the slowest response. Active-chat
  transcript loading is already detached from shell boot; preserve that ordering.
- `web/src/App.vue` shows startup UI on every mount until a startup-status response,
  then fades it out for 400 ms. The startup request has no abort deadline, unlike
  the engine monitor's probe. This is a potential warm-load delay, even when the
  engine is already ready.
- `/api/chats` includes archived rows at boot and probes Claude session availability
  per chat. Project listing also performs vault discovery synchronously. Their
  effect on a large real installation remains unmeasured; the global Claude
  session lookup already has a cache, so do not reintroduce that solved bottleneck.

## Implementation plan

Implementation scope approved on 2026-10-11: static JS/CSS compression, optional
chat/viewer loading with chat-entry prefetching, and bounded startup probing with
a short display grace period. A Luna subagent implements these changes; the
parent agent reviews response negotiation, async component lifecycle, startup
recovery, and tests before running the full gates and browser comparisons.
Catalog redesign remains conditional on a separate large-catalog measurement.
The running engine will not be restarted or deployed as part of this change.

### 1 Verify the deployed waterfall and compress static assets

Inspect `Content-Encoding`, `Vary`, transferred size, and cache headers at the
origin where loading feels slow. Capture cold and warm loads of Home and an existing
chat, distinguishing document TTFB, asset download, startup check, and data requests.
If the proxy already compresses assets, start with step 2 instead.

For direct engine serving, add negotiated gzip to the static asset serving path,
with a sensible constant minimum size. Keep hashed asset caching and entry-point
revalidation policies intact. Start with JS/CSS compression; compare the actual
Starlette path rather than assuming the Node prototype's timings transfer exactly.
Evaluate build-time precompression if runtime CPU cost is material. Keep this
change scoped to static responses so chat streaming and private API behavior do
not acquire unrelated changes.

Acceptance: gzip-capable requests receive decodable compressed content and
`Vary: Accept-Encoding`; uncompressed requests remain valid; immutable hashes,
MIME types, conditional requests, security headers, and SPA deep links work.
The synthetic mobile cold Home median should be at most 3.2 s with compression
alone. This is a proposed regression budget, not a production guarantee.

### 2 Split unused features while preserving chat entry speed

Load the chat panel and pinned viewer when their view needs them. Load the file
modal on first open; consider retaining it after first use if unmounting changes
focus restoration, edit state, or cleanup behavior. Inspect its watchers before
using the prototype's simple `v-if` in production.

For a chat deep link, begin downloading chat code alongside bootstrap data rather
than waiting for `activeChat` to resolve. Use route intent after the normal auth
gate. Do not prefetch the chat panel on every Home visit. Preserve component refs
used by global keyboard shortcuts and the order between transcript loading and
WebSocket connection.

Acceptance: compressed completed Home JS/CSS at readiness stays below 256,000
bytes (250 KiB; raised from 250,000 on 2026-10-21 — readable model labels and
the header naming work measured 250,411, and the old number had no headroom, see
issue #1792);
synthetic mobile Home median stays at or below 2.6 s. Chat composer readiness
must stay within 100 ms of compression alone under the same conditions. Validate
first chat opening, active and archived deep links, first file opening, pins,
keyboard shortcuts, focus restoration, and failed lazy-chunk loads.

### 3 Reduce startup waits using the real warm-load trace

Give startup probing a bounded lifetime and coordinate it with engine monitoring
so a hanging request cannot leave the startup curtain indefinitely. Evaluate a
brief delayed display for startup UI, allowing an already-ready engine to avoid a
flash and exit animation. Keep genuine boot, restart, auth, and outage states
explicit; do not infer engine readiness from a persisted previous visit.

The one-second fault experiments show that auth, startup, and chats can each
constrain readiness. Their added delays are not additive because startup and
route work overlap. Address whichever request dominates the deployed warm trace.
Do not remove the signed-session check to shorten the waterfall.

Acceptance: a ready local engine avoids a gratuitous startup overlay; delayed,
failed, and hanging probes still show accurate recovery states. Cover cold boot,
restart, login, engine loss, and Safari resume. Compare both reduced-motion and
ordinary motion, since the latter currently adds the 400 ms fade.

### 4 Bound catalog work only if larger fixtures confirm it matters

Add synthetic catalogs with 100, 1,000, and 10,000 archived chats. Time serialization,
session-availability checks, network transfer, and frontend reconciliation
separately. Measure event-loop responsiveness while project discovery runs.
If catalog work dominates, design an active/recent initial catalog and demand-loaded
history, or a bounded asynchronous backend read. Preserve archive browsing,
cross-workspace awareness, search, unread state, and exact deep-link lookup.
Simply changing boot to `active_only=1` would drop required history behavior.

This step has no claimed speedup yet. Avoid cache layers or catalog redesigns
until the measurements justify them. Images are low priority: the two face PNGs
are only about 10–11 KB each, and the app uses system fonts.

## Validation and follow through

Use isolated production builds and invented fixtures for before/after comparisons.
Run at least five cold and five warm samples in implementation PRs; retain the
waterfall and separate useful Home/chat readiness from visual paint metrics.
Recheck a real iPhone PWA over the operator's network, including foreground reload
and background resume. Add stable bundle-size checks; avoid fragile single-run
wall-clock gates on shared CI runners.

Run focused behavior tests and all blocking checks before pushing:
`mypy ciao`, `pytest -n auto tests/`, `cd web && npm test`, and
`cd web && npm run build` for frontend changes. Run the relevant browser regression
journeys and visually inspect Home, Chat, and the first-open viewer on desktop and
mobile. Verify focus, zoom, touch targets, shared shortcuts, picker precedence,
and IME handling. The scoped production optimization is implemented in this
working tree; the catalog redesign remains conditional on further measurement.

Suggested GitHub tracking issue: **[Agent] Reduce cold page loading time and
measure warm startup delays**. Include this plan's fixture results and require
the installed-origin trace before attributing the operator's delay to compression.

## Guidance and experiment artifacts

The plan follows Vue's guidance to measure actual builds and load optional features
on demand, and web.dev's guidance to compress text assets and analyze rendering
delays separately from resource download:

- [Vue performance guidance](https://vuejs.org/guide/best-practices/performance)
- [Vue Router lazy loading](https://router.vuejs.org/guide/advanced/lazy-loading)
- [Text asset encoding and transfer](https://web.dev/articles/optimizing-content-efficiency-optimize-encoding-and-transfer)
- [Largest Contentful Paint analysis](https://web.dev/articles/optimize-lcp)
- [Starlette middleware documentation](https://www.starlette.io/middleware/)

Portable measurement summaries are in
[`performance/page-load-results.json`](performance/page-load-results.json) and
[`performance/page-load-implementation-results.json`](performance/page-load-implementation-results.json).
Temporary scripts, raw resource timings, isolated builds, and screenshots are in
`/tmp/ciao-load-study/` and `/tmp/ciao-load-implementation/` for this session.
The scripts launch the synthetic fixture,
apply CDP cache/network/CPU controls, and check the stated readiness selectors.
They do not change the running service.
