// @vitest-environment node
//
// Behavioural coverage for the in-frame comment bridge. The script itself is a
// string in `ciao/web/artifact_bridge.py` (it ships inlined in the artifact
// response), so it has no import seam — this test reads it out of the Python
// module and runs it in a real DOM.
//
// That cross-language read is deliberate. The bridge is the one piece of this
// feature no other suite can reach: the Python tests can only assert on the
// text of the script, and the Vue tests stop at the postMessage boundary
// because the frame is an opaque origin. Anchoring, highlight reapplication,
// and the ready handshake were all silently broken while both suites were
// green, so they are asserted here against actual DOM behaviour.
//
// If the extraction below stops finding the script, the marker in
// artifact_bridge.py changed — fix the marker, do not delete the test.
import { readFileSync } from 'node:fs'
import { JSDOM } from 'jsdom'
import { beforeEach, describe, expect, it } from 'vitest'

const BRIDGE_SOURCE = new URL('../../../ciao/web/artifact_bridge.py', import.meta.url)

function readBridgeScript(): string {
  const py = readFileSync(BRIDGE_SOURCE, 'utf8')
  const open = 'BRIDGE_SCRIPT = r"""'
  const start = py.indexOf(open)
  expect(start, 'BRIDGE_SCRIPT assignment not found in artifact_bridge.py').toBeGreaterThan(-1)
  const from = start + open.length
  const end = py.indexOf('"""', from)
  expect(end, 'unterminated BRIDGE_SCRIPT string').toBeGreaterThan(-1)
  return py.slice(from, end)
}

const SCRIPT = readBridgeScript()

const DOC = `<!DOCTYPE html><html><head></head><body>
  <div class="card">
    <h2>Revenue
       rose sharply</h2>
    <p>First part</p><p>Second part</p>
    <p><a href="https://example.com/gone">Linked label</a></p>
  </div>
  <svg width="100" height="100"><rect x="1" y="1" width="10" height="10"></rect></svg>
</body></html>`

type Posted = Record<string, unknown>

interface Harness {
  window: Window & typeof globalThis
  doc: Document
  posted: Posted[]
  apply: (comments: unknown[]) => Promise<void>
  enable: () => Promise<void>
  tick: (ms?: number) => Promise<void>
}

function boot(): Harness {
  const posted: Posted[] = []
  const { window } = new JSDOM(DOC, { runScripts: 'outside-only', pretendToBeVisual: true })

  // `window.parent` is a non-writable accessor in jsdom, and the bridge posts
  // through it.
  Object.defineProperty(window, 'parent', {
    value: { postMessage: (p: Posted) => posted.push(p) },
    configurable: true,
  })
  // jsdom has no layout engine, so these do not exist at all. The bridge only
  // uses them to place its pill, so fixed boxes are enough.
  const box = { left: 10, right: 60, top: 20, bottom: 40, width: 50, height: 20, x: 10, y: 20, toJSON: () => ({}) } as DOMRect
  window.Range.prototype.getBoundingClientRect = () => box
  window.Element.prototype.getBoundingClientRect = () => box

  window.eval(SCRIPT)

  const tick = (ms = 30): Promise<void> => new Promise((r) => setTimeout(r, ms))
  return {
    window,
    doc: window.document,
    posted,
    tick,
    // The compose half is inert until a parent proves it is listening, which
    // is what HtmlArtifactViewer does on the ready handshake.
    enable: async () => {
      window.postMessage({ frame: 'ciao-artifact', type: 'ciao:comments-enable' }, '*')
      await tick(20)
    },
    apply: async (comments: unknown[]) => {
      window.postMessage({ frame: 'ciao-artifact', type: 'ciao:apply-comments', comments }, '*')
      await tick(20)
    },
  }
}

describe('artifact bridge: ready handshake', () => {
  it('defers ready until the DOM is parsed', async () => {
    // The script is injected into <head>, so posting ready during the parse
    // would have the parent push highlights at a tree with no <body> — every
    // selector would miss and the marks would silently never appear.
    const h = boot()
    expect(h.doc.readyState).toBe('loading')
    expect(h.posted.some((p) => p.action === 'ready')).toBe(false)

    await h.tick()

    expect(h.posted.filter((p) => p.action === 'ready')).toHaveLength(1)
  })
})

describe('artifact bridge: compose handshake', () => {
  it('floats no pill until a parent proves it is listening', async () => {
    // An artifact served to something that cannot receive the anchors must not
    // show a Comment pill: it reads as a working feature and does nothing.
    const h = boot()
    await h.tick()

    const ps = h.doc.querySelectorAll('.card p')
    const range = h.doc.createRange()
    range.selectNodeContents(ps[0])
    const sel = h.window.getSelection()
    sel?.removeAllRanges()
    sel?.addRange(range)
    h.doc.dispatchEvent(new h.window.Event('selectionchange'))
    await h.tick(250)

    expect(h.doc.getElementById('ciao-comment-pill')).toBeNull()

    await h.enable()
    h.doc.dispatchEvent(new h.window.Event('selectionchange'))
    await h.tick(250)

    expect(h.doc.getElementById('ciao-comment-pill')).not.toBeNull()
  })

  it('accepts a highlight push as proof on its own', async () => {
    // An integrated parent that pushes highlights needs no extra call.
    const h = boot()
    await h.tick()
    await h.apply([])

    const ps = h.doc.querySelectorAll('.card p')
    const range = h.doc.createRange()
    range.selectNodeContents(ps[0])
    const sel = h.window.getSelection()
    sel?.removeAllRanges()
    sel?.addRange(range)
    h.doc.dispatchEvent(new h.window.Event('selectionchange'))
    await h.tick(250)

    expect(h.doc.getElementById('ciao-comment-pill')).not.toBeNull()
  })
})

describe('artifact bridge: highlight reapplication', () => {
  let h: Harness

  beforeEach(async () => {
    h = boot()
    await h.tick()
  })

  it('matches a quote whose whitespace was collapsed at capture time', async () => {
    // Quotes are stored with /\s+/ collapsed to single spaces, but
    // el.textContent keeps the document's newlines and indentation, so a plain
    // indexOf misses every multi-line selection.
    await h.apply([
      {
        id: 'c1',
        selector: 'body > div:nth-of-type(1) > h2:nth-of-type(1)',
        quote: 'Revenue rose sharply',
      },
    ])

    const mark = h.doc.querySelector('mark.ciao-comment-mark')
    expect(mark).not.toBeNull()
    expect(mark?.textContent).toContain('rose sharply')
    expect(mark?.getAttribute('data-ciao-comment-id')).toBe('c1')
  })

  it('outlines a whole-element anchor that has no text to quote', async () => {
    // The case Alt+Click exists for: an SVG shape or chart bar. A <mark>
    // around text cannot anchor it, and gating on a non-empty quote dropped
    // the comment entirely.
    await h.apply([
      {
        id: 'c2',
        selector: 'body > svg:nth-of-type(1) > rect:nth-of-type(1)',
        quote: '',
        wholeElement: true,
      },
    ])

    const rect = h.doc.querySelector('rect')
    expect(rect?.getAttribute('class')).toContain('ciao-comment-el')
    expect(rect?.getAttribute('data-ciao-comment-id')).toBe('c2')
  })

  it('clears both text marks and element outlines on the next push', async () => {
    await h.apply([
      { id: 'c1', selector: 'body > div:nth-of-type(1) > h2:nth-of-type(1)', quote: 'Revenue rose sharply' },
      { id: 'c2', selector: 'body > svg:nth-of-type(1) > rect:nth-of-type(1)', quote: '', wholeElement: true },
    ])
    expect(h.doc.querySelectorAll('mark.ciao-comment-mark').length).toBeGreaterThan(0)
    expect(h.doc.querySelector('.ciao-comment-el')).not.toBeNull()

    await h.apply([])

    expect(h.doc.querySelector('mark.ciao-comment-mark')).toBeNull()
    expect(h.doc.querySelector('.ciao-comment-el')).toBeNull()
    expect(h.doc.querySelector('rect')?.hasAttribute('data-ciao-comment-id')).toBe(false)
  })

  it('keeps every id when two comments share one element', async () => {
    // Overwriting the attribute per comment left the earlier comment with no
    // clickable anchor at all.
    const selector = 'body > svg:nth-of-type(1) > rect:nth-of-type(1)'
    await h.apply([
      { id: 'c1', selector, quote: '', wholeElement: true },
      { id: 'c2', selector, quote: '', wholeElement: true },
    ])

    const rect = h.doc.querySelector('rect') as Element
    expect(rect.getAttribute('data-ciao-comment-id')).toBe('c1 c2')

    // Repeated clicks walk the list, so both comments are reachable.
    h.posted.length = 0
    rect.dispatchEvent(new h.window.MouseEvent('click', { bubbles: true }))
    rect.dispatchEvent(new h.window.MouseEvent('click', { bubbles: true }))
    rect.dispatchEvent(new h.window.MouseEvent('click', { bubbles: true }))
    expect(h.posted.filter((p) => p.action === 'open').map((p) => p.id)).toEqual(['c1', 'c2', 'c1'])
  })

  it('consumes a click on a highlight instead of letting the link fire', async () => {
    // A highlight can sit inside a link or a button; letting the default
    // action through would navigate the frame out from under the comment.
    await h.apply([
      {
        id: 'c1',
        selector: 'body > div:nth-of-type(1) > p:nth-of-type(3) > a:nth-of-type(1)',
        quote: 'Linked label',
      },
    ])
    const mark = h.doc.querySelector('mark.ciao-comment-mark') as Element
    expect(mark).not.toBeNull()

    const click = new h.window.MouseEvent('click', { bubbles: true, cancelable: true })
    mark.dispatchEvent(click)

    expect(click.defaultPrevented).toBe(true)
    expect(h.posted.some((p) => p.action === 'open' && p.id === 'c1')).toBe(true)
  })

  it('ignores messages that are not the apply-comments protocol', async () => {
    await h.apply([{ id: 'c1', selector: 'body > div:nth-of-type(1) > h2:nth-of-type(1)', quote: 'Revenue rose sharply' }])
    h.window.postMessage({ frame: 'other', type: 'ciao:apply-comments', comments: [] }, '*')
    h.window.postMessage({ frame: 'ciao-artifact', type: 'something-else', comments: [] }, '*')
    await h.tick(20)

    // Still there: neither message reached applyComments.
    expect(h.doc.querySelector('mark.ciao-comment-mark')).not.toBeNull()
  })
})

describe('artifact bridge: selection anchoring', () => {
  it('anchors a selection that crosses an element boundary to a common ancestor', async () => {
    // Anchoring to the start container's parent left the end offset pointing
    // outside the anchor element (setEnd collapses the range rather than
    // throwing), so only the first fragment could be re-highlighted.
    const h = boot()
    await h.tick()
    await h.enable()

    const ps = h.doc.querySelectorAll('.card p')
    const range = h.doc.createRange()
    range.setStart(ps[0].firstChild as Node, 6)
    range.setEnd(ps[1].firstChild as Node, 6)
    const sel = h.window.getSelection()
    sel?.removeAllRanges()
    sel?.addRange(range)

    h.posted.length = 0
    h.doc.dispatchEvent(new h.window.Event('selectionchange'))
    await h.tick(250)

    const pill = h.doc.getElementById('ciao-comment-pill')
    expect(pill, 'the Comment pill should float for a cross-element selection').not.toBeNull()
    pill?.dispatchEvent(new h.window.MouseEvent('click', { bubbles: true }))

    const payload = h.posted.find((p) => p.action === 'compose') as Record<string, string | number>
    expect(payload).toBeDefined()
    expect(payload.selector).toBe('body > div:nth-of-type(1)')

    // The offsets must address the quote inside the element they are stored
    // against, which is the whole point of picking the common ancestor.
    const anchorEl = h.doc.querySelector(payload.selector as string)
    const text = anchorEl?.textContent ?? ''
    const slice = text.slice(payload.startOffset as number, payload.endOffset as number)
    expect(slice.replace(/\s+/g, ' ').trim()).toBe(payload.quote)

    // And reapplication marks every fragment the selection touched.
    await h.apply([
      {
        id: 'c3',
        selector: payload.selector,
        quote: payload.quote,
        startOffset: payload.startOffset,
        endOffset: payload.endOffset,
      },
    ])
    expect(h.doc.querySelectorAll('mark.ciao-comment-mark')).toHaveLength(2)
  })

  it('gives the Comment pill a 44px touch box', async () => {
    // On touch the pill is how a selection becomes a comment, and 13px text
    // plus padding alone made it ~24px tall (AGENTS.md touch-target rule).
    // jsdom has no layout, so this asserts the rule reaches the element
    // rather than a measured height.
    const h = boot()
    await h.tick()
    await h.enable()
    const ps = h.doc.querySelectorAll('.card p')
    const range = h.doc.createRange()
    range.selectNodeContents(ps[0])
    const sel = h.window.getSelection()
    sel?.removeAllRanges()
    sel?.addRange(range)
    h.doc.dispatchEvent(new h.window.Event('selectionchange'))
    await h.tick(250)

    const pill = h.doc.getElementById('ciao-comment-pill')
    expect(pill).not.toBeNull()
    const style = h.window.getComputedStyle(pill as Element)
    expect(style.minHeight).toBe('44px')
    expect(style.minWidth).toBe('44px')
  })

  it('posts a whole-element anchor on Alt+Click, textless nodes included', async () => {
    const h = boot()
    await h.tick()
    await h.enable()
    h.posted.length = 0

    const rect = h.doc.querySelector('rect') as Element
    rect.dispatchEvent(new h.window.MouseEvent('click', { bubbles: true, altKey: true }))

    const payload = h.posted.find((p) => p.action === 'compose') as Record<string, unknown>
    expect(payload).toBeDefined()
    expect(payload.wholeElement).toBe(true)
    expect(payload.elementTag).toBe('rect')
    expect(payload.quote).toBe('')
    // Body-anchored so the path cannot match a same-shaped chain elsewhere.
    expect(payload.selector).toBe('body > svg:nth-of-type(1) > rect:nth-of-type(1)')
  })
})
