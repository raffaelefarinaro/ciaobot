---
name: html-artifact
description: Build a self-contained interactive HTML page (an artifact) that Ciaobot renders live in the pinned panel. Use when the output is easier to look at than to read: dashboards, charts, annotated diffs, side-by-side option comparisons, timelines, interactive mockups, calculators, diagrams drawn as inline SVG, or anything the user wants to make visual, interactive, clickable, or "a page". Trigger on "artifact", "dashboard", "chart", "graph", "visualize this", "make it interactive", "build me a page", "show it side by side", "architecture", "flow diagram", "sequence". Do NOT use for prose reports (.md) or tabular data (.csv).
---

# HTML artifacts

An artifact is one self-contained `.html` file in the workspace. Ciaobot renders it in the pinned panel next to the chat, with a Preview/Code toggle, and the user can open it full-window in a real browser.

Write the file, then call `file_surface` on it. Writing alone paints an inline card; only `file_surface` opens the panel.

## Use an artifact when

- The output is visual or spatial: a chart, a timeline, a diff with annotations, a layout, a floor plan
- The reader benefits from interaction: filter a table, expand a section, switch tabs, toggle a scenario, run a small calculation
- Several things need to sit side by side and be compared at a glance
- A diagram is the answer: an architecture, a flow, a sequence. Draw it as inline SVG. See the "Static SVG diagram" section in `reference.md`.
- The user asks for a dashboard, a page, or "something I can click through"

## Do not use an artifact when

Ciaobot's other formats have integrations an artifact silently loses. Check this list before reaching for HTML.

| Content | Use | Why not HTML |
|---|---|---|
| Prose: reports, plans, specs, notes | `.md` | Wikilinks, backlinks, frontmatter, inline editing, and **comments**. Comments anchor to durable markdown text; artifact comments anchor to a page revision (a selector + quoted text that stops re-finding its spot after the page is rewritten). |
| Tabular data | `.csv` | Renders as a sortable table with per-cell comments. |

Diagrams have no `.excalidraw` path here: draw them as inline SVG inside the artifact.

If the answer is mostly words with one small illustration, write markdown. An artifact is for when the visual *is* the answer.

## Hard constraints

The page is served under a strict Content Security Policy in a sandboxed frame. These are not style preferences; break one and the artifact renders blank or half-dead with no visible error.

- **One file.** No sibling CSS, JS, or image files. No relative links: nothing is deployed next to the page, so `href="./other.html"` goes nowhere. Use in-page anchors for navigation.
- **Everything inline.** `<style>` and `<script>` in the document. No CDN, no `<script src="https://…">`, no Google Fonts link, no Tailwind CDN. External requests are blocked.
- **No network at runtime.** `fetch`, `XMLHttpRequest`, WebSocket, and EventSource are all blocked. Bake the data into the page as a JS literal. If the data needs to be fresh, that is a job for a chat turn that rewrites the artifact, not for the artifact.
- **No forms.** `form-action` is blocked and there is no backend. Inputs that drive in-page JS are fine; submitting is not.
- **Images**: inline SVG, or CSS, or a `data:` URI. Remote image URLs are blocked.
- **Fonts**: system font stack, or a `data:` URI. Use `font-family: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif`.
- **No animation loops.** No `setInterval` redraw, no `requestAnimationFrame` loop, no long-running computation. The frame shares a process with the app, so a page that keeps computing slows the whole chat. Respond to clicks, then be still.
- **Under 2 MB.** That is the render cap, the source-view cap, and the snapshot cap. Over it the panel shows a 413 and the page has to be opened in a browser instead. Data URIs for raster images are what blow this; prefer SVG.

## Make it fit the panel

- Include `<meta name="viewport" content="width=device-width, initial-scale=1">`. The panel is narrow and there is a phone viewer.
- Support both colour schemes with `@media (prefers-color-scheme: dark)`, or the artifact glares white next to the dark app.
- Design for roughly 420px wide first and let it grow. Single column, wrapping flex, `minmax()` grids. A fixed 1200px layout is unreadable in the panel.
- Keep text selectable and legible: 14px minimum, real contrast.

## Design

Avoid the defaults that make a page look machine-made: purple-to-blue gradients, everything centered, uniform 12px rounded corners on every box, Inter everywhere, a wall of equally-weighted cards. Pick one accent colour, use weight and spacing for hierarchy, and let the layout follow the data instead of the reverse.

Numbers carry their source. A figure with no label, unit, or date reads as invented.

Static SVG diagrams follow the same rules plus the anti-machine-made ones above: one accent reserved for 1-2 focal points, shape carries type (oval for start/end, diamond for decision), orthogonal arrows drawn behind nodes, and a legend. `reference.md` has a starting shell.

## Where to write it

- Project work: the project's vault folder.
- One-off: `Workspace/`.
- Name it for the content, `deploy-failures.html`, not `artifact.html`. Update the existing file when iterating rather than creating `-v2`.

## Iterating

Editing the file and calling `file_surface` again is enough; the panel reloads the frame when the turn ends. The user can comment on the rendered page (select text, or Alt+Click an element) and the comment rides on the next chat message. After a revision the frame reloads and old comment highlights are re-applied by quoted text where they still match; an Alt+Click comment on a textless node (an SVG shape, a chart bar) re-appears as an outline on that element. The user can also edit the source directly from the panel's Code view, and every write is snapshotted, so History and Diff work like any other file.

## Checklist before surfacing

1. One file, no external requests of any kind
2. No `fetch`/`setInterval`/forms
3. Viewport meta, dark-mode media query, works at 420px
4. Under 2 MB
5. Every number labelled
6. `file_surface` called

## More

`reference.md` has three starting shells (metric dashboard with an SVG bar chart, tabbed comparison, timeline) and `assets/minichart.js` is a small inlinable chart helper for bars, lines, and donuts. Read `reference.md` when starting a new artifact rather than deriving the scaffolding again.
