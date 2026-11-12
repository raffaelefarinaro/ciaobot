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
  select: (range: Range) => Promise<void>
}

/** Minimal CSS Custom Highlight API: what Chromium and WebKit give the bridge. */
class FakeHighlight {
  ranges: Range[] = []
  add(range: Range): void { this.ranges.push(range) }
}

const BOX = { left: 10, right: 60, top: 20, bottom: 40, width: 50, height: 20, x: 10, y: 20, toJSON: () => ({}) } as DOMRect

function boot(options: { html?: string; highlightApi?: boolean } = {}): Harness {
  const posted: Posted[] = []
  const { window } = new JSDOM(options.html ?? DOC, { runScripts: 'outside-only', pretendToBeVisual: true })

  // `window.parent` is a non-writable accessor in jsdom, and the bridge posts
  // through it.
  Object.defineProperty(window, 'parent', {
    value: { postMessage: (p: Posted) => posted.push(p) },
    configurable: true,
  })
  // jsdom has no layout engine, so these do not exist at all. The bridge only
  // uses them to report where a selection ends and to hit-test clicks on
  // painted ranges, so fixed boxes are enough.
  window.Range.prototype.getBoundingClientRect = () => BOX
  window.Range.prototype.getClientRects = (() => [BOX]) as unknown as Range['getClientRects']
  window.Element.prototype.getBoundingClientRect = () => BOX
  if (options.highlightApi) {
    Object.defineProperty(window, 'CSS', { value: { highlights: new Map() }, configurable: true })
    Object.defineProperty(window, 'Highlight', { value: FakeHighlight, configurable: true })
  }

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
    // Select a range and let the 150ms selectionchange debounce settle.
    select: async (range: Range) => {
      const sel = window.getSelection()
      sel?.removeAllRanges()
      sel?.addRange(range)
      window.document.dispatchEvent(new window.Event('selectionchange'))
      await tick(250)
    },
  }
}

function selections(h: Harness): Posted[] {
  return h.posted.filter((p) => p.action === 'selection')
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
  it('reports no selection until a parent proves it is listening', async () => {
    // An artifact served to something that cannot receive the anchors must
    // not swallow keystrokes on behalf of a composer that does not exist.
    const h = boot()
    await h.tick()

    const range = h.doc.createRange()
    range.selectNodeContents(h.doc.querySelectorAll('.card p')[0])
    await h.select(range)
    expect(selections(h)).toHaveLength(0)

    await h.enable()
    h.doc.dispatchEvent(new h.window.Event('selectionchange'))
    await h.tick(250)

    expect(selections(h)).toHaveLength(1)
  })

  it('accepts a highlight push as proof on its own', async () => {
    // An integrated parent that pushes highlights needs no extra call.
    const h = boot()
    await h.tick()
    await h.apply([])

    const range = h.doc.createRange()
    range.selectNodeContents(h.doc.querySelectorAll('.card p')[0])
    await h.select(range)

    expect(selections(h)).toHaveLength(1)
  })
})

describe('artifact bridge: selection reporting', () => {
  let h: Harness

  beforeEach(async () => {
    h = boot()
    await h.tick()
    await h.enable()
  })

  it('draws no Comment pill of its own', async () => {
    // The page renders the same Comment button it uses for Markdown; a second
    // pill inside the frame looked and behaved differently.
    const range = h.doc.createRange()
    range.selectNodeContents(h.doc.querySelectorAll('.card p')[0])
    await h.select(range)

    expect(h.doc.querySelector('button')).toBeNull()
    expect(h.doc.getElementById('ciao-comment-pill')).toBeNull()
  })

  it('reports a selection once, with its anchor and end box, and its collapse', async () => {
    const range = h.doc.createRange()
    range.selectNodeContents(h.doc.querySelectorAll('.card p')[0])
    await h.select(range)
    // The same selection again: nothing new to say.
    h.doc.dispatchEvent(new h.window.Event('selectionchange'))
    await h.tick(250)

    expect(selections(h)).toEqual([
      expect.objectContaining({
        frame: 'ciao-artifact',
        type: 'ciao:artifact-comment',
        selector: 'body > div:nth-of-type(1) > p:nth-of-type(1)',
        quote: 'First part',
        startOffset: 0,
        endOffset: 10,
        elementTag: 'p',
        rect: { top: 20, left: 10, bottom: 40, right: 60 },
      }),
    ])

    h.window.getSelection()?.removeAllRanges()
    h.doc.dispatchEvent(new h.window.Event('selectionchange'))
    await h.tick(250)
    expect(h.posted.filter((m) => m.action === 'selection-cleared')).toHaveLength(1)
  })

  it('re-reports the selection when the frame scrolls, so the button follows', async () => {
    const range = h.doc.createRange()
    range.selectNodeContents(h.doc.querySelectorAll('.card p')[0])
    await h.select(range)
    expect(selections(h)).toHaveLength(1)

    h.window.dispatchEvent(new h.window.Event('scroll'))
    await h.tick(50)

    expect(selections(h)).toHaveLength(2)
  })

  it('drops the native selection when the page asks', async () => {
    const range = h.doc.createRange()
    range.selectNodeContents(h.doc.querySelectorAll('.card p')[0])
    await h.select(range)

    h.window.postMessage({ frame: 'ciao-artifact', type: 'ciao:clear-selection' }, '*')
    await h.tick(250)

    expect(h.window.getSelection()?.isCollapsed).toBe(true)
    // The page asked, so it already knows: no echo.
    expect(h.posted.filter((m) => m.action === 'selection-cleared')).toHaveLength(0)
    // And a key no longer has a selection to comment on.
    const key = new h.window.KeyboardEvent('keydown', { key: 'a', bubbles: true, cancelable: true })
    h.doc.body.dispatchEvent(key)
    expect(key.defaultPrevented).toBe(false)
  })
})

describe('artifact bridge: type-to-comment', () => {
  let h: Harness

  async function selectFirstParagraph(): Promise<void> {
    const range = h.doc.createRange()
    range.selectNodeContents(h.doc.querySelectorAll('.card p')[0])
    await h.select(range)
  }

  function press(key: string, init: KeyboardEventInit = {}, target: EventTarget = h.doc.body): KeyboardEvent {
    const e = new h.window.KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...init })
    target.dispatchEvent(e)
    return e
  }

  beforeEach(async () => {
    h = boot()
    await h.tick()
    await h.enable()
  })

  it('forwards a printable key typed with a selection and swallows it', async () => {
    // The page's type-to-comment listens on the page's document; keys typed
    // with focus in the frame never reached it, so typing did nothing.
    await selectFirstParagraph()
    const e = press('R')

    expect(e.defaultPrevented).toBe(true)
    expect(h.posted.filter((m) => m.action === 'type')).toEqual([
      expect.objectContaining({ frame: 'ciao-artifact', type: 'ciao:artifact-comment', text: 'R' }),
    ])
  })

  it('leaves keys alone without a selection, with modifiers, for space and mid-IME', async () => {
    expect(press('a').defaultPrevented).toBe(false)

    await selectFirstParagraph()
    expect(press('c', { metaKey: true }).defaultPrevented).toBe(false)
    expect(press('c', { ctrlKey: true }).defaultPrevented).toBe(false)
    expect(press('a', { altKey: true }).defaultPrevented).toBe(false)
    expect(press(' ').defaultPrevented).toBe(false)
    expect(press('ArrowDown').defaultPrevented).toBe(false)
    expect(press('a', { isComposing: true }).defaultPrevented).toBe(false)
    expect(h.posted.filter((m) => m.action === 'type')).toHaveLength(0)
  })

  it('never takes keys from a field inside the artifact', async () => {
    const input = h.doc.createElement('input')
    h.doc.body.appendChild(input)
    await selectFirstParagraph()

    expect(press('a', {}, input).defaultPrevented).toBe(false)
    expect(h.posted.filter((m) => m.action === 'type')).toHaveLength(0)
  })

  it('stays out of the way until a parent is listening', async () => {
    const quiet = boot()
    await quiet.tick()
    const range = quiet.doc.createRange()
    range.selectNodeContents(quiet.doc.querySelectorAll('.card p')[0])
    await quiet.select(range)
    const e = new quiet.window.KeyboardEvent('keydown', { key: 'a', bubbles: true, cancelable: true })
    quiet.doc.body.dispatchEvent(e)

    expect(e.defaultPrevented).toBe(false)
    expect(quiet.posted.filter((m) => m.action === 'type')).toHaveLength(0)
  })

  it('comments on the selection the key followed, even inside the debounce', async () => {
    await selectFirstParagraph()
    // A new selection, and a key before the 150ms debounce reports it.
    const range = h.doc.createRange()
    range.selectNodeContents(h.doc.querySelectorAll('.card p')[1])
    const sel = h.window.getSelection()
    sel?.removeAllRanges()
    sel?.addRange(range)
    h.doc.dispatchEvent(new h.window.Event('selectionchange'))
    press('x')

    const reported = selections(h)
    expect(reported[reported.length - 1]?.quote).toBe('Second part')
    // The report went out before the keystroke, so the page composes on it.
    const lastSelectionAt = h.posted.lastIndexOf(reported[reported.length - 1])
    const typeAt = h.posted.findIndex((m) => m.action === 'type')
    expect(typeAt).toBeGreaterThan(lastSelectionAt)
  })

  it('forwards pasted text', async () => {
    await selectFirstParagraph()
    const paste = new h.window.Event('paste', { bubbles: true, cancelable: true })
    Object.defineProperty(paste, 'clipboardData', {
      value: { getData: (kind: string) => (kind === 'text/plain' ? 'pasted note' : ''), files: [] },
    })
    h.doc.body.dispatchEvent(paste)

    expect(paste.defaultPrevented).toBe(true)
    expect(h.posted.filter((m) => m.action === 'type').map((m) => m.text)).toEqual(['pasted note'])
  })

  function paste(text: string, files: unknown[] = []): Event {
    const e = new h.window.Event('paste', { bubbles: true, cancelable: true })
    Object.defineProperty(e, 'clipboardData', {
      value: { getData: (kind: string) => (kind === 'text/plain' ? text : ''), files },
    })
    h.doc.body.dispatchEvent(e)
    return e
  }

  it('never forwards pasted files, and caps pasted text', async () => {
    // The page would upload forwarded files as comment images with no user
    // action, and a forged message could do the same.
    await selectFirstParagraph()
    const image = new h.window.File(['x'], 'shot.png', { type: 'image/png' })
    expect(paste('', [image]).defaultPrevented).toBe(false)
    expect(h.posted.filter((m) => m.action === 'type')).toHaveLength(0)

    paste('y'.repeat(25_000))
    const sent = h.posted.filter((m) => m.action === 'type')
    expect(sent).toHaveLength(1)
    expect((sent[0].text as string).length).toBe(10_000)
    expect(sent[0]).not.toHaveProperty('files')
  })

  it('forwards one key per selection, then the burst until the page answers', async () => {
    await selectFirstParagraph()
    expect(press('a').defaultPrevented).toBe(true)
    // The composer is opening but has no focus yet: the next letters belong
    // to the same note.
    expect(press('b').defaultPrevented).toBe(true)

    // The page answered: the burst is over and the selection is spent.
    h.window.postMessage({ frame: 'ciao-artifact', type: 'ciao:clear-selection' }, '*')
    await h.tick(20)
    expect(press('c').defaultPrevented).toBe(false)
    expect(h.posted.filter((m) => m.action === 'type').map((m) => m.text)).toEqual(['a', 'b'])
  })

  it('spends the selection on the forwarded key', async () => {
    // The page opened a composer, which was then cancelled; the old selection
    // must not keep turning keystrokes into new comments.
    await selectFirstParagraph()
    press('a')
    for (const busy of [true, false]) {
      h.window.postMessage({ frame: 'ciao-artifact', type: 'ciao:comments-busy', busy }, '*')
      await h.tick(20)
    }
    expect(press('b').defaultPrevented).toBe(false)
    expect(h.posted.filter((m) => m.action === 'type').map((m) => m.text)).toEqual(['a'])
  })

  it('takes no keys or pastes while the page has a composer open', async () => {
    h.window.postMessage({ frame: 'ciao-artifact', type: 'ciao:comments-busy', busy: true }, '*')
    await h.tick(20)
    await selectFirstParagraph()
    expect(press('a').defaultPrevented).toBe(false)
    expect(paste('note').defaultPrevented).toBe(false)

    h.window.postMessage({ frame: 'ciao-artifact', type: 'ciao:comments-busy', busy: false }, '*')
    await h.tick(20)
    expect(press('a').defaultPrevented).toBe(true)
  })

  it('treats Safari keyCode 229 as IME composition', async () => {
    await selectFirstParagraph()
    const e = new h.window.KeyboardEvent('keydown', { key: 'n', bubbles: true, cancelable: true })
    Object.defineProperty(e, 'keyCode', { value: 229 })
    h.doc.body.dispatchEvent(e)
    expect(e.defaultPrevented).toBe(false)
  })

  it('keeps a forwarded key from the artifact\'s own handlers', async () => {
    const seen: string[] = []
    h.window.addEventListener('keydown', (e) => seen.push(e.key), true)
    h.doc.addEventListener('keydown', (e) => seen.push(e.key))
    await selectFirstParagraph()
    press('k')
    expect(seen).toEqual([])
  })
})

describe('artifact bridge: resize', () => {
  it('re-reports the selection when the frame resizes, so the button follows', async () => {
    const h = boot()
    await h.tick()
    await h.enable()
    const range = h.doc.createRange()
    range.selectNodeContents(h.doc.querySelectorAll('.card p')[0])
    await h.select(range)

    h.window.dispatchEvent(new h.window.Event('resize'))
    await h.tick(50)

    expect(selections(h)).toHaveLength(2)
  })
})

describe('artifact bridge: tables', () => {
  // Pretty-printed markup: the indentation between the <th>s is text nodes
  // that are direct children of the <tr>.
  const TABLE = `<!DOCTYPE html><html><head></head><body>
  <table>
    <thead>
      <tr>
        <th>Name</th>
        <th>Owner</th>
        <th>Status</th>
      </tr>
    </thead>
    <tbody>
      <tr><td>a</td><td>b</td><td>c</td></tr>
    </tbody>
  </table>
</body></html>`

  async function commentAcrossHeaders(h: Harness): Promise<HTMLTableRowElement> {
    await h.tick()
    await h.enable()
    const ths = h.doc.querySelectorAll('th')
    const range = h.doc.createRange()
    range.setStart(ths[0].firstChild as Node, 0)
    range.setEnd(ths[2].firstChild as Node, 6)
    await h.select(range)
    const s = selections(h)[0]
    expect(s, 'a selection across header cells should be reported').toBeDefined()
    await h.apply([{ id: 't1', selector: s.selector, quote: s.quote, startOffset: s.startOffset, endOffset: s.endOffset }])
    return h.doc.querySelector('thead tr') as HTMLTableRowElement
  }

  it('paints across header cells without touching the row (Highlight API)', async () => {
    const h = boot({ html: TABLE, highlightApi: true })
    const before = h.doc.querySelector('thead tr')!.childNodes.length
    const tr = await commentAcrossHeaders(h)

    expect(tr.childNodes.length).toBe(before)
    expect(h.doc.querySelector('mark')).toBeNull()
    const painted = (h.window as unknown as { CSS: { highlights: Map<string, FakeHighlight> } })
      .CSS.highlights.get('ciao-comment')
    // One range per cell's text: the indentation between cells is left out.
    expect(painted?.ranges.map((r) => r.toString())).toEqual(['Name', 'Owner', 'Status'])
  })

  it('never puts a <mark> in table structure (fallback)', async () => {
    // A <mark> directly under a <tr> renders as an anonymous cell: the header
    // row gained a phantom column and every later cell shifted.
    const h = boot({ html: TABLE })
    const before = h.doc.querySelector('thead tr')!.childNodes.length
    const tr = await commentAcrossHeaders(h)

    expect(tr.childNodes.length).toBe(before)
    const marks = Array.from(h.doc.querySelectorAll('mark.ciao-comment-mark'))
    expect(marks.map((m) => m.parentElement?.tagName)).toEqual(['TH', 'TH', 'TH'])
    expect(marks.map((m) => m.textContent)).toEqual(['Name', 'Owner', 'Status'])
  })
})

describe('artifact bridge: CSS Custom Highlight API', () => {
  let h: Harness
  const H2 = 'body > div:nth-of-type(1) > h2:nth-of-type(1)'

  function highlights(): Map<string, FakeHighlight> {
    return (h.window as unknown as { CSS: { highlights: Map<string, FakeHighlight> } }).CSS.highlights
  }

  beforeEach(async () => {
    h = boot({ highlightApi: true })
    await h.tick()
  })

  it('paints text comments without changing the document', async () => {
    const before = h.doc.body.innerHTML
    await h.apply([{ id: 'c1', selector: H2, quote: 'Revenue rose sharply' }])

    expect(h.doc.body.innerHTML).toBe(before)
    expect(highlights().get('ciao-comment')?.ranges[0].toString()).toMatch(/Revenue\s+rose sharply/)
    // The pseudo-element is styled, or the highlight would be invisible.
    const style = h.doc.querySelector('style[data-ciao-bridge-style]')?.textContent ?? ''
    expect(style).toContain('::highlight(ciao-comment)')

    await h.apply([])
    expect(highlights().has('ciao-comment')).toBe(false)
  })

  it('opens the comment under a click on a painted range and consumes the click', async () => {
    await h.apply([{ id: 'c1', selector: H2, quote: 'Revenue rose sharply' }])
    h.posted.length = 0

    const hit = new h.window.MouseEvent('click', { bubbles: true, cancelable: true, clientX: 30, clientY: 30 })
    h.doc.querySelector('h2')!.dispatchEvent(hit)
    expect(hit.defaultPrevented).toBe(true)
    expect(h.posted.filter((p) => p.action === 'open').map((p) => p.id)).toEqual(['c1'])

    const miss = new h.window.MouseEvent('click', { bubbles: true, cancelable: true, clientX: 500, clientY: 500 })
    h.doc.querySelector('h2')!.dispatchEvent(miss)
    expect(miss.defaultPrevented).toBe(false)
    expect(h.posted.filter((p) => p.action === 'open')).toHaveLength(1)
  })

  it('leaves controls and bare space inside a commented span clickable', async () => {
    // A range that contains whole elements reports their full border boxes
    // from getClientRects, so hit-testing the comment's one big range took
    // every click anywhere in the commented paragraphs, buttons included.
    const multi = boot({
      highlightApi: true,
      html: `<!DOCTYPE html><html><head></head><body>
        <div id="box"><p>Alpha text</p><p>Beta <button>Go</button> text</p></div>
      </body></html>`,
    })
    await multi.tick()
    const BORDER_BOX = { left: 0, right: 2000, top: 0, bottom: 2000, width: 2000, height: 2000, x: 0, y: 0, toJSON: () => ({}) }
    multi.window.Range.prototype.getClientRects = function (this: Range) {
      const textOnly = this.startContainer === this.endContainer && this.startContainer.nodeType === 3
      return [textOnly ? BOX : BORDER_BOX]
    } as unknown as Range['getClientRects']
    const text = multi.doc.getElementById('box')!.textContent!
    await multi.apply([{ id: 'c1', selector: '#box', quote: text.replace(/\s+/g, ' ').trim(), startOffset: 0, endOffset: text.length }])
    multi.posted.length = 0

    const button = multi.doc.querySelector('button')!
    let pressed = 0
    button.addEventListener('click', () => pressed++)
    const onButton = new multi.window.MouseEvent('click', { bubbles: true, cancelable: true, clientX: 500, clientY: 500 })
    button.dispatchEvent(onButton)
    expect(onButton.defaultPrevented).toBe(false)
    expect(pressed).toBe(1)

    // Bare space in the paragraph, away from any of its text.
    const onSpace = new multi.window.MouseEvent('click', { bubbles: true, cancelable: true, clientX: 500, clientY: 500 })
    multi.doc.querySelectorAll('p')[1].dispatchEvent(onSpace)
    expect(onSpace.defaultPrevented).toBe(false)
    expect(multi.posted.filter((p) => p.action === 'open')).toHaveLength(0)

    // The text itself still opens the comment.
    const onText = new multi.window.MouseEvent('click', { bubbles: true, cancelable: true, clientX: 30, clientY: 30 })
    multi.doc.querySelectorAll('p')[0].dispatchEvent(onText)
    expect(multi.posted.filter((p) => p.action === 'open').map((p) => p.id)).toEqual(['c1'])
  })

  it('paints the draft but never opens it on click', async () => {
    await h.apply([{ id: '__draft__', selector: H2, quote: 'Revenue rose sharply', draft: true }])
    expect(highlights().get('ciao-comment')?.ranges).toHaveLength(1)

    const click = new h.window.MouseEvent('click', { bubbles: true, cancelable: true, clientX: 30, clientY: 30 })
    h.doc.querySelector('h2')!.dispatchEvent(click)
    expect(click.defaultPrevented).toBe(false)
    expect(h.posted.filter((p) => p.action === 'open')).toHaveLength(0)
  })

  it('shows a pointer over painted text', async () => {
    // ::highlight cannot set a cursor, so the bridge hit-tests on mousemove.
    await h.enable()
    await h.apply([{ id: 'c1', selector: H2, quote: 'Revenue rose sharply' }])
    const root = h.doc.documentElement
    h.doc.querySelector('h2')!.dispatchEvent(new h.window.MouseEvent('mousemove', { bubbles: true, clientX: 30, clientY: 30 }))
    await h.tick(50)
    expect(root.classList.contains('ciao-comment-hover')).toBe(true)

    h.doc.body.dispatchEvent(new h.window.MouseEvent('mousemove', { bubbles: true, clientX: 500, clientY: 500 }))
    await h.tick(50)
    expect(root.classList.contains('ciao-comment-hover')).toBe(false)
  })

  it('still outlines whole-element anchors', async () => {
    await h.apply([{ id: 'c2', selector: 'body > svg:nth-of-type(1) > rect:nth-of-type(1)', quote: '', wholeElement: true }])
    expect(h.doc.querySelector('rect')?.getAttribute('class')).toContain('ciao-comment-el')
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
    h.posted.length = 0
    await h.select(range)

    const payload = selections(h)[0] as Record<string, string | number>
    expect(payload, 'a cross-element selection should be reported').toBeDefined()
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

describe('artifact bridge: over-length selections', () => {
  // A selection longer than MAX_QUOTE (500) is stored quote-truncated while
  // the offsets still span the whole thing, so the stored prefix must be
  // recognised as truncated and the offsets trusted. The "is it truncated?"
  // test used to run on the COLLAPSED quote, and collapse() trims — so when
  // the 500-char slice happened to end on a space the quote came back 499,
  // the check said "not truncated", and the highlight covered only the
  // stored prefix instead of the user's full selection.
  const WORD = 'aaaa '            // 5 chars, so every index 5n+4 is a space
  const FULL = WORD.repeat(160)   // 800 chars; index 499 is a space
  const STORED = FULL.slice(0, 500)

  it('places the space exactly at the cap boundary', () => {
    // Guards the fixture itself: if this stops holding the test below proves
    // nothing, because the off-by-one only bites when the slice ends on space.
    expect(FULL.length).toBe(800)
    expect(STORED.length).toBe(500)
    expect(STORED.endsWith(' ')).toBe(true)
    expect(STORED.trim().length).toBe(499)
  })

  it('highlights the whole selection, not just the stored 500-char prefix', async () => {
    const h = boot()
    await h.tick()
    const p = h.doc.createElement('p')
    p.id = 'long'
    p.textContent = FULL
    h.doc.body.appendChild(p)

    await h.apply([
      {
        id: 'long1',
        selector: '#long',
        quote: STORED,
        startOffset: 0,
        endOffset: FULL.length,
      },
    ])

    const mark = h.doc.querySelector('mark.ciao-comment-mark')
    expect(mark).not.toBeNull()
    // The offsets were trusted, so the mark spans the entire selection.
    // Before the fix this was ~500.
    expect(mark!.textContent!.trim().length).toBeGreaterThan(700)
  })
})

describe('artifact bridge: deep-element anchors', () => {
  // cssPath is length-bounded. Once it drops outer ancestors the remaining
  // '>' chain matches the same tag/index shape anywhere, so a whole-element
  // anchor on a deep node used to outline the FIRST element in document order
  // with that inner shape — a different card/row, with nothing to detect it.
  function deepDoc(): string {
    // Two identical sibling cards. The target is inside the SECOND one, deep
    // enough that a bounded path loses the segment telling them apart.
    // Deep enough to overrun MAX_SELECTOR (400): each level costs
    // `figcaption:nth-of-type(1) > ` = 29 chars, so 14 of them blows the
    // budget inside the 16-level walk and the outermost segments — the ones
    // naming WHICH section — are the ones dropped.
    const DEPTH = 14
    const inner = (label: string) =>
      '<figcaption>'.repeat(DEPTH) +
      `<span>${label}</span>` +
      '</figcaption>'.repeat(DEPTH)
    return `<!DOCTYPE html><html><body>
      <section>${inner('first card')}</section>
      <section>${inner('second card')}</section>
    </body></html>`
  }

  it('never anchors an element comment to a different element', async () => {
    const { window } = new JSDOM(deepDoc(), { runScripts: 'outside-only', pretendToBeVisual: true })
    const posted: Record<string, unknown>[] = []
    Object.defineProperty(window, 'parent', {
      value: { postMessage: (p: Record<string, unknown>) => posted.push(p) },
      configurable: true,
    })
    const box = { left: 10, right: 60, top: 20, bottom: 40, width: 50, height: 20, x: 10, y: 20, toJSON: () => ({}) } as DOMRect
    window.Range.prototype.getBoundingClientRect = () => box
    window.Element.prototype.getBoundingClientRect = () => box
    window.eval(SCRIPT)
    await new Promise((r) => setTimeout(r, 30))
    window.postMessage({ frame: 'ciao-artifact', type: 'ciao:comments-enable' }, '*')
    await new Promise((r) => setTimeout(r, 20))

    const target = window.document.querySelectorAll('span')[1]
    expect(target.textContent).toBe('second card')
    target.dispatchEvent(new window.MouseEvent('click', { altKey: true, bubbles: true, cancelable: true }))
    await new Promise((r) => setTimeout(r, 20))

    const compose = posted.find((p) => p.action === 'compose')
    if (compose) {
      // Whatever selector it chose must resolve to the clicked element or one
      // of its ancestors — never to a node in the other card.
      const found = window.document.querySelector(compose.selector as string)
      expect(found).not.toBeNull()
      expect(found!.contains(target) || found === target).toBe(true)
    }
    // Declining outright is also acceptable; anchoring elsewhere is not.
  })
})
