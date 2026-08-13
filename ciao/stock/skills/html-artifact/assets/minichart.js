/* minichart — tiny SVG chart helper for Ciaobot HTML artifacts.
 *
 * Paste the whole file inside a <script> tag in the artifact. It returns SVG
 * markup as a string; there is no DOM dependency, no network, and no timers,
 * so it satisfies the artifact CSP.
 *
 * Values must be finite and non-negative. Non-numeric or negative entries are
 * dropped rather than silently drawn wrong.
 *
 *   document.getElementById('chart').innerHTML = miniChart.bars({
 *     data: [{ label: 'auth', value: 12 }, { label: 'api', value: 31 }],
 *     accent: '#e0492f',
 *     unit: ' failures',
 *   })
 */
const miniChart = (() => {
  const esc = (s) =>
    String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c])

  const clean = (data) =>
    (Array.isArray(data) ? data : [])
      .map((d) => ({ label: d && d.label != null ? String(d.label) : '', value: Number(d && d.value) }))
      .filter((d) => Number.isFinite(d.value) && d.value >= 0)

  // Round an axis maximum up to 1, 2 or 5 times a power of ten, so ticks land
  // on numbers a reader recognises instead of 37.4.
  function niceMax(raw) {
    if (!(raw > 0)) return 1
    const pow = Math.pow(10, Math.floor(Math.log10(raw)))
    const n = raw / pow
    const step = n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10
    return step * pow
  }

  const fmt = (v, unit) => {
    const n = Math.abs(v) >= 1000 ? v.toLocaleString('en-US') : String(Math.round(v * 100) / 100)
    return n + (unit || '')
  }

  function frame(w, h, body, title) {
    return (
      `<svg viewBox="0 0 ${w} ${h}" width="100%" height="${h}" role="img" ` +
      `aria-label="${esc(title || 'chart')}" font-family="system-ui, sans-serif" font-size="11">` +
      body +
      '</svg>'
    )
  }

  /* Horizontal bars. Best default for named categories: labels stay readable
   * however long they are, which is where vertical bars fall apart. */
  function bars(opts = {}) {
    const data = clean(opts.data)
    if (!data.length) return '<p>No data.</p>'
    const accent = opts.accent || '#4f8cff'
    const gutter = Math.min(160, Math.max(...data.map((d) => d.label.length)) * 6.5 + 8)
    const rowH = 22
    const w = opts.width || 480
    const h = data.length * rowH + 8
    const max = niceMax(Math.max(...data.map((d) => d.value)))
    const track = w - gutter - 56

    const rows = data.map((d, i) => {
      const y = i * rowH + 4
      const len = max > 0 ? (d.value / max) * track : 0
      return (
        `<text x="${gutter - 6}" y="${y + 12}" text-anchor="end" fill="currentColor" opacity="0.75">${esc(d.label)}</text>` +
        `<rect x="${gutter}" y="${y + 2}" width="${track}" height="13" fill="currentColor" opacity="0.07" rx="2"/>` +
        `<rect x="${gutter}" y="${y + 2}" width="${len.toFixed(1)}" height="13" fill="${esc(accent)}" rx="2"/>` +
        `<text x="${gutter + track + 6}" y="${y + 12}" fill="currentColor" opacity="0.9">${esc(fmt(d.value, opts.unit))}</text>`
      )
    })
    return frame(w, h, rows.join(''), opts.title)
  }

  /* Line chart over ordered points. Draws min/max reference labels only, since
   * a full axis costs more room than it earns at panel width. */
  function line(opts = {}) {
    const data = clean(opts.data)
    if (data.length < 2) return '<p>Need at least two points.</p>'
    const accent = opts.accent || '#4f8cff'
    const w = opts.width || 480
    const h = opts.height || 160
    const padL = 40
    const padB = 22
    const max = niceMax(Math.max(...data.map((d) => d.value)))
    const stepX = (w - padL - 8) / (data.length - 1)
    const plotH = h - padB - 12

    const pts = data.map((d, i) => {
      const x = padL + i * stepX
      const y = 12 + plotH - (max > 0 ? (d.value / max) * plotH : 0)
      return [x, y]
    })
    const path = pts.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(1)} ${y.toFixed(1)}`).join(' ')
    const area =
      `${path} L${pts[pts.length - 1][0].toFixed(1)} ${(12 + plotH).toFixed(1)} ` +
      `L${pts[0][0].toFixed(1)} ${(12 + plotH).toFixed(1)} Z`

    // First, last, and the peak: the labels a reader actually looks for.
    const peak = data.reduce((best, d, i) => (d.value > data[best].value ? i : best), 0)
    const marks = [0, peak, data.length - 1]
      .filter((i, idx, arr) => arr.indexOf(i) === idx)
      .map((i) => {
        const [x, y] = pts[i]
        const anchor = i === 0 ? 'start' : i === data.length - 1 ? 'end' : 'middle'
        return (
          `<circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="3" fill="${esc(accent)}"/>` +
          `<text x="${x.toFixed(1)}" y="${(y - 7).toFixed(1)}" text-anchor="${anchor}" fill="currentColor">` +
          `${esc(fmt(data[i].value, opts.unit))}</text>` +
          `<text x="${x.toFixed(1)}" y="${h - 6}" text-anchor="${anchor}" fill="currentColor" opacity="0.6">` +
          `${esc(data[i].label)}</text>`
        )
      })

    return frame(
      w,
      h,
      `<text x="0" y="16" fill="currentColor" opacity="0.6">${esc(fmt(max, opts.unit))}</text>` +
        `<text x="0" y="${12 + plotH}" fill="currentColor" opacity="0.6">0</text>` +
        `<line x1="${padL}" y1="${12 + plotH}" x2="${w - 8}" y2="${12 + plotH}" stroke="currentColor" opacity="0.2"/>` +
        `<path d="${area}" fill="${esc(accent)}" opacity="0.12"/>` +
        `<path d="${path}" fill="none" stroke="${esc(accent)}" stroke-width="2" stroke-linejoin="round"/>` +
        marks.join(''),
      opts.title,
    )
  }

  /* Donut for parts of a whole. Anything under 2% of the total is folded into
   * the last slice rather than drawn as an invisible sliver. */
  function donut(opts = {}) {
    const data = clean(opts.data)
    const total = data.reduce((s, d) => s + d.value, 0)
    if (!total) return '<p>No data.</p>'
    const palette = opts.palette || ['#4f8cff', '#e0492f', '#f2b53a', '#3ea36b', '#8b5cf6', '#94a3b8']
    const size = opts.size || 150
    const r = size / 2 - 6
    const cx = size / 2
    const cy = size / 2
    const thickness = opts.thickness || 18

    let angle = -Math.PI / 2
    const arcs = []
    const legend = []
    data.forEach((d, i) => {
      const frac = d.value / total
      const sweep = frac * Math.PI * 2
      const colour = palette[i % palette.length]
      if (frac >= 0.02) {
        const x1 = cx + r * Math.cos(angle)
        const y1 = cy + r * Math.sin(angle)
        const x2 = cx + r * Math.cos(angle + sweep)
        const y2 = cy + r * Math.sin(angle + sweep)
        arcs.push(
          `<path d="M${x1.toFixed(1)} ${y1.toFixed(1)} A${r} ${r} 0 ${sweep > Math.PI ? 1 : 0} 1 ${x2.toFixed(1)} ${y2.toFixed(1)}" ` +
            `fill="none" stroke="${esc(colour)}" stroke-width="${thickness}"/>`,
        )
      }
      angle += sweep
      legend.push(
        `<span style="display:inline-flex;align-items:center;gap:5px;margin-right:10px">` +
          `<i style="width:9px;height:9px;border-radius:2px;background:${esc(colour)}"></i>` +
          `${esc(d.label)} ${Math.round(frac * 100)}%</span>`,
      )
    })

    const centre = opts.centre != null ? String(opts.centre) : fmt(total, opts.unit)
    const svg = frame(
      size,
      size,
      arcs.join('') +
        `<text x="${cx}" y="${cy + 4}" text-anchor="middle" font-size="15" font-weight="600" fill="currentColor">${esc(centre)}</text>`,
      opts.title,
    )
    return `<div style="max-width:${size}px">${svg}</div><div style="font-size:11px;margin-top:4px">${legend.join('')}</div>`
  }

  return { bars, line, donut, niceMax, fmt }
})()
