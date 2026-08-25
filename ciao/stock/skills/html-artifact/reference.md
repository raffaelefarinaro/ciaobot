# Artifact starting shells

Three shells that already satisfy the CSP and the panel constraints. Copy one, replace the data and the content, delete what you do not use. Do not re-derive this scaffolding.

Every shell assumes:

- `<meta name="viewport">` present
- one accent colour, set once in `--accent`
- light and dark both handled through `color-scheme` plus one media query
- no external requests, no timers, no forms

To use the chart helper, paste the contents of `assets/minichart.js` inside a `<script>` tag before your own script. It draws bars, a line, and a donut as SVG strings. For a single simple chart, hand-written SVG is often shorter; use the helper when there are several charts or the data changes shape.

## Base shell

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Deploy failures by service</title>
<style>
  :root { color-scheme: light dark; --accent: #e0492f; --bg: #fff; --fg: #1a1a1a; --muted: #666; --line: #e3e3e3; --card: #fafafa; }
  @media (prefers-color-scheme: dark) {
    :root { --bg: #16181d; --fg: #e8e8f0; --muted: #9aa0ad; --line: #2a2e39; --card: #1c1f26; }
  }
  * { box-sizing: border-box; }
  body { margin: 0; padding: 18px; background: var(--bg); color: var(--fg);
         font: 14px/1.55 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
  h1 { font-size: 19px; margin: 0 0 2px; }
  .sub { color: var(--muted); font-size: 12px; margin-bottom: 18px; }
  .card { background: var(--card); border: 1px solid var(--line); border-radius: 6px; padding: 12px 14px; }
  .grid { display: grid; gap: 10px; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); }
  .n { font-size: 22px; font-weight: 600; }
  .n small { font-size: 11px; font-weight: 400; color: var(--muted); display: block; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  th, td { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--line); }
  th { color: var(--muted); font-weight: 500; font-size: 11px; text-transform: uppercase; letter-spacing: .04em; }
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
</style>
</head>
<body>
  <h1>Deploy failures by service</h1>
  <p class="sub">14 runs, 2026-08-04 to 2026-08-11. Source: CI job logs.</p>

  <div class="grid">
    <div class="card n">43<small>failures</small></div>
    <div class="card n">6<small>services affected</small></div>
    <div class="card n">31%<small>of runs</small></div>
  </div>

  <div class="card" style="margin-top:14px">
    <div id="chart"></div>
  </div>

<script>
const DATA = [
  { label: 'api-gateway', value: 18 },
  { label: 'auth', value: 12 },
  { label: 'billing', value: 7 },
  { label: 'search', value: 6 },
];
/* paste assets/minichart.js here, then: */
document.getElementById('chart').innerHTML = miniChart.bars({ data: DATA, accent: '#e0492f', unit: ' fails' });
</script>
</body>
</html>
```

## Tabbed comparison

For putting options side by side. Radio inputs plus CSS mean zero JS and no state to lose. Add the base shell's `<style>` above this.

```html
<style>
  .tabs { display: flex; gap: 4px; border-bottom: 1px solid var(--line); margin: 14px 0 0; }
  .tabs label { padding: 7px 12px; font-size: 13px; color: var(--muted); cursor: pointer;
                border: 1px solid transparent; border-bottom: none; border-radius: 5px 5px 0 0; }
  .tabs input { position: absolute; opacity: 0; pointer-events: none; }
  .tabs input:checked + label { color: var(--fg); background: var(--card);
                                border-color: var(--line); font-weight: 600; }
  .pane { display: none; padding: 14px; border: 1px solid var(--line); border-top: none;
          border-radius: 0 0 6px 6px; background: var(--card); }
  #t-a:checked ~ .panes #p-a, #t-b:checked ~ .panes #p-b { display: block; }
</style>

<div class="tabbed">
  <div class="tabs">
    <input type="radio" name="opt" id="t-a" checked><label for="t-a">On device</label>
    <input type="radio" name="opt" id="t-b"><label for="t-b">On server</label>
  </div>
  <div class="panes">
    <div class="pane" id="p-a"><h2>On device</h2><p>…</p></div>
    <div class="pane" id="p-b"><h2>On server</h2><p>…</p></div>
  </div>
</div>
```

The `#t-a:checked ~ .panes #p-a` rule needs the inputs to be siblings of `.panes`, which is why the inputs sit outside the `.pane` elements. Add one selector per tab.

## Timeline

For an investigation log or a sequence of events. Reads well at panel width because it stays one column.

```html
<style>
  .tl { list-style: none; margin: 14px 0 0; padding: 0 0 0 18px; border-left: 2px solid var(--line); }
  .tl li { position: relative; padding: 0 0 16px 14px; }
  .tl li::before { content: ''; position: absolute; left: -25px; top: 4px; width: 9px; height: 9px;
                   border-radius: 50%; background: var(--accent); }
  .tl time { display: block; font-size: 11px; color: var(--muted); font-variant-numeric: tabular-nums; }
  .tl b { display: block; margin: 1px 0 2px; }
  .tl .note { font-size: 13px; color: var(--muted); }
</style>

<ol class="tl">
  <li>
    <time>09:14</time>
    <b>First 502s on api-gateway</b>
    <span class="note">Error rate 0.2% to 14% in under a minute.</span>
  </li>
  <li>
    <time>09:31</time>
    <b>Rollback started</b>
    <span class="note">Deploy 4f21c reverted.</span>
  </li>
</ol>
```

## Interaction without a framework

Filtering or toggling is a few lines of vanilla JS. Render from an array, re-render on input. No timers, no fetch.

```html
<input id="q" type="search" placeholder="Filter services" style="padding:6px 8px;width:100%;max-width:260px">
<tbody id="rows"></tbody>

<script>
const rows = document.getElementById('rows');
function render(filter = '') {
  const f = filter.trim().toLowerCase();
  rows.innerHTML = DATA
    .filter(d => !f || d.label.toLowerCase().includes(f))
    .map(d => `<tr><td>${d.label}</td><td class="num">${d.value}</td></tr>`)
    .join('') || '<tr><td colspan="2">No matches.</td></tr>';
}
document.getElementById('q').addEventListener('input', e => render(e.target.value));
render();
</script>
```

Building HTML with a template string is fine here because the data is yours, baked into the page at author time. If any string could come from somewhere else, set `textContent` instead.

## Static SVG diagram

For an architecture, a flow, or a sequence. Builds on the base shell's tokens (light/dark handled, one accent).

Conventions that keep the diagram readable and not machine-made:

- **Shape carries type.** Oval = start/end, rectangle = step, diamond = decision, thin-bordered zone = grouping container.
- **One accent, two focal points max.** Reserve it for the primary path or the single thing under review; everything else is muted ink/muted outline.
- **Arrows behind the nodes.** Draw the edges first so node fills cover the line ends, then the boxes, then labels.
- **Orthogonal arrows.** Run lines only horizontally and vertically; route around nodes with `H`/`V` path segments, not diagonals.
- **Legend.** A short strip at the bottom decoding the shape/line/colour grammar, so the diagram argues on its own.
- **Accessible name.** `role="img"` plus a `<title>` and a one-line `<desc>` describing what the diagram shows, with IDs that stay unique if several SVG sit on one page.

One panel trap this shell fixes that bare SVG falls into: the panel is ~420px, so no `min-width` on the SVG and no fixed 900px design. Use a `viewBox` and let it scale down, or stack vertically. The `<defs>` for the arrow markers are inlined; nothing is fetched.

```html
<style>
  .dg { margin: 14px 0; }
  .dg svg { width: 100%; display: block; }
  .dg .eyebrow { color: var(--muted); font-size: 11px; text-transform: uppercase;
                 letter-spacing: .08em; margin-bottom: 4px; }
</style>

<div class="dg">
  <p class="eyebrow">Architecture</p>
  <svg viewBox="0 0 640 360" role="img" aria-labelledby="dg-title dg-desc">
    <title id="dg-title">Reader request path</title>
    <desc id="dg-desc">A request moves from the reader through the edge cache to the origin, then to the CMS.</desc>
    <defs>
      <marker id="arrow" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto">
        <polygon points="0 0, 8 3, 0 6" fill="var(--muted)"/>
      </marker>
      <marker id="arrow-accent" markerWidth="8" markerHeight="6" refX="7" refY="3" orient="auto">
        <polygon points="0 0, 8 3, 0 6" fill="var(--accent)"/>
      </marker>
    </defs>

    <!-- arrows first, then boxes, then labels -->
    <line x1="180" y1="180" x2="252" y2="180" stroke="var(--muted)" stroke-width="1.2" marker-end="url(#arrow)"/>
    <line x1="388" y1="180" x2="460" y2="180" stroke="var(--accent)" stroke-width="1.4" marker-end="url(#arrow-accent)"/>

    <rect x="60" y="140" width="120" height="80" rx="40" fill="var(--card)" stroke="var(--muted)" stroke-width="1"/>
    <text x="120" y="184" fill="var(--fg)" font-size="14" font-weight="600" text-anchor="middle">Reader</text>

    <rect x="252" y="148" width="136" height="64" rx="6" fill="var(--card)" stroke="var(--muted)" stroke-width="1"/>
    <text x="320" y="176" fill="var(--fg)" font-size="13" font-weight="600" text-anchor="middle">Edge cache</text>
    <text x="320" y="192" fill="var(--muted)" font-size="11" text-anchor="middle">CDN</text>

    <rect x="460" y="148" width="136" height="64" rx="6" fill="var(--card)" stroke="var(--accent)" stroke-width="1"/>
    <text x="528" y="176" fill="var(--fg)" font-size="13" font-weight="600" text-anchor="middle">Origin</text>
    <text x="528" y="192" fill="var(--muted)" font-size="11" text-anchor="middle">SSR + MDX</text>

    <!-- zone container -->
    <rect x="460" y="300" width="136" height="52" rx="6" fill="var(--card)" stroke="var(--muted)" stroke-width="1"/>
    <text x="528" y="330" fill="var(--fg)" font-size="13" font-weight="600" text-anchor="middle">CMS</text>

    <line x1="40" y1="360" x2="600" y2="360" stroke="var(--line)" stroke-width="1"/>
    <rect x="40" y="372" width="14" height="10" rx="2" fill="var(--card)" stroke="var(--accent)" stroke-width="1"/>
    <text x="62" y="382" fill="var(--muted)" font-size="11">Focal path</text>
  </svg>
</div>
```

Colour the SVG with `var()` tokens so the dark-mode media query keeps working. If the diagram is dense enough that 420px gets cramped, keep the SVG full-width and let it scroll vertically inside the panel rather than shrinking the type below 11px. Build a flow chart the same way: an oval start, rectangle steps, a diamond for the decision, and a labelled legend at the bottom that says what each shape and line means.
