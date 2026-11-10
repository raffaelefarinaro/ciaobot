"""In-frame comment bridge for HTML artifact previews.

An artifact renders in a sandboxed iframe (opaque origin, ``allow-scripts``,
no same-origin access). The PWA parent cannot reach into that document, and
the document cannot reach the parent — except through ``postMessage``, which
works across origins in both directions.

This module holds the small script injected into every artifact response by
``workspace_html`` (see ``inject_bridge``). The script runs inside the frame
and is a thin agent: it reports selections and clicks and paints highlights,
while the PWA page owns the Comment button, the composer and the keyboard.
That split is Ghostwriter's (``app/web/static/frame.js``), and it is what makes
an artifact comment look and behave like a Markdown one: the button is the
page's own ``.fv-comment-trigger``, not a second pill drawn inside the frame.

- It watches ``selectionchange`` and reports each distinct non-empty selection
  once as ``action: 'selection'`` — the anchor (a CSS selector path plus
  character offsets into that element's text, and the quote) and the box of
  the selection's end in frame-viewport coordinates — and ``'selection-cleared'``
  when it collapses. It re-reports on scroll so the page's button follows.
- While a selection is reported, a printable keystroke or a paste inside the
  frame is swallowed and posted as ``action: 'type'``: the page's
  type-to-comment listener lives on the page's document and never sees keys
  typed into the frame, so the frame has to forward them.
- Alt-clicking any element still posts ``action: 'compose'`` for the element
  itself rather than its text.
- It re-applies durable comment highlights on request: the parent sends a
  ``ciao:apply-comments`` message after the frame loads or the comment list
  changes. Text highlights are painted with the CSS Custom Highlight API, which
  leaves the document's DOM alone; wrapping text in ``<mark>`` is only the
  fallback for an engine without it, and even then never inside table
  structure (a ``<mark>`` directly under a ``<tr>`` renders as an extra cell).
  A whole-element anchor outlines the element instead — the node it points at
  (an SVG shape, a chart bar) may have no text to wrap at all.
- A click on a highlight is consumed (the highlight can sit inside a link or
  a button, whose default action would otherwise fire too) and opens one
  comment. Painted ranges are not elements, so clicks are hit-tested against
  their client rects. Several comments can share one spot, so repeated clicks
  walk the list.
- It posts ``action: 'ready'`` once the DOM is parsed. That is the handshake
  the parent waits for before its first highlight push: the script itself runs
  from ``<head>``, so anything pushed earlier would query a tree with no
  ``<body>`` and silently match nothing.

Handshake: the compose half (selection reports, typing and Alt+Click) is inert
until the parent posts ``ciao:comments-enable`` or any ``ciao:apply-comments``.
Swallowing keystrokes for a page that is not listening would break typing in
the artifact for nothing, so the bridge stays quiet until a parent has proved
it is listening. Highlight re-application is unaffected: it only ever acts on
comments the parent itself sent. ``ciao:clear-selection`` drops the native
selection once the page has opened its composer.

Trust model: the script is our code, but it shares a document with
model-authored script, which could forge the same messages. That is
acceptable — the worst case is a pending comment appearing in the composer,
which the user reviews before sending. The bridge makes no network requests
(impossible under the artifact CSP anyway) and touches nothing but the
highlights it owns.
"""

from __future__ import annotations

import re

# Marker attribute on the injected script tag; also the idempotency check.
_BRIDGE_MARKER = "data-ciao-artifact-bridge"

_HEAD_RE = re.compile(r"<head\b[^>]*>", re.IGNORECASE)
_BODY_RE = re.compile(r"<body\b[^>]*>", re.IGNORECASE)
_HTML_RE = re.compile(r"<html\b[^>]*>", re.IGNORECASE)
_DOCTYPE_RE = re.compile(r"<!doctype\b[^>]*>", re.IGNORECASE)

# Plain ES5: the frame is a WKWebView/Chromium webview, but nothing here needs
# anything newer, and older webviews in the desktop shell are a real audience.
BRIDGE_SCRIPT = r"""(function () {
  if (window.__ciaoArtifactBridge) return
  window.__ciaoArtifactBridge = true

  // Fallback-only: text highlights are <mark> elements just when the engine
  // has no CSS Custom Highlight API (see paintRanges).
  var MARK_CLASS = 'ciao-comment-mark'
  // Name of the CSS highlight registered for text comments.
  var HIGHLIGHT_NAME = 'ciao-comment'
  // Whole-element anchors cannot be a highlight around text: the case they
  // exist for is a node with no text at all (an SVG shape, a chart bar). They
  // are an outline on the element itself instead, so they stay visible and
  // clickable.
  var EL_CLASS = 'ciao-comment-el'
  // Space-separated: several comments can anchor to the same whole element,
  // and storing only the last one left the earlier ones unclickable.
  var ID_ATTR = 'data-ciao-comment-id'
  // Which id the next click on this anchor should open (see nextId).
  var CURSOR_ATTR = 'data-ciao-comment-cursor'
  var MAX_QUOTE = 500
  var MAX_SELECTOR = 400
  // A text node directly under one of these is table structure, not cell
  // content: a <mark> there becomes an anonymous table cell and shifts the
  // row by a column.
  var TABLE_STRUCTURE = /^(TABLE|THEAD|TBODY|TFOOT|TR|COLGROUP)$/
  // One user-perceived printable character: a single UTF-16 unit or one
  // surrogate pair (what Array.from(key).length === 1 accepts on the page).
  var ONE_CHAR = /^(?:[\uD800-\uDBFF][\uDC00-\uDFFF]|[\s\S])$/

  function hasHighlightApi() {
    return !!(window.CSS && window.CSS.highlights && typeof window.Highlight === 'function')
  }

  var css = document.createElement('style')
  css.setAttribute('data-ciao-bridge-style', '')
  css.textContent =
    'mark.' + MARK_CLASS + '{background:rgba(251,146,60,.28);color:inherit;' +
    'border-radius:2px;cursor:pointer;box-decoration-break:clone;-webkit-box-decoration-break:clone}' +
    '.' + EL_CLASS + '{outline:2px solid rgba(251,146,60,.85);outline-offset:1px;cursor:pointer}' +
    // Last, and alone: an engine that cannot parse the pseudo-element drops
    // just this rule.
    '::highlight(' + HIGHLIGHT_NAME + '){background-color:rgba(251,146,60,.28)}'
  ;(document.head || document.documentElement).appendChild(css)

  function post(payload) {
    payload.frame = 'ciao-artifact'
    try { window.parent.postMessage(payload, '*') } catch (e) { /* parent gone */ }
  }

  function clampLen(value, max) {
    if (typeof value !== 'string') return ''
    return value.length > max ? value.slice(0, max) : value
  }

  // ── Anchor building ─────────────────────────────────────────────────
  // A stable-enough CSS path: tag + nth-of-type from the element up to body.
  // Not resilient to structural rewrites of the artifact — the quoted text is
  // the durable part of the anchor for the model; the selector only has to
  // re-find the highlight until the next revision reloads the frame.
  // Length-bounded by whole segments. A deep tree with long tag names
  // (`figcaption:nth-of-type(12) > ` is 29 chars) overruns MAX_SELECTOR well
  // inside the 16-level walk, and clamping the joined string cut a segment in
  // half: `document.querySelector` then either threw or matched some other
  // element, so the comment was stored with a permanently unresolvable anchor
  // and no error anywhere. Dropping outer ancestors instead keeps the selector
  // valid — less specific, which the quote check in `resolveSpan` covers.
  function cssPath(el) {
    var parts = []
    var length = 0
    var node = el
    var depth = 0
    var anchored = false
    while (node && node.nodeType === 1 && node !== document.body && depth < 16) {
      var tag = node.tagName.toLowerCase()
      var index = 1
      var sib = node.previousElementSibling
      while (sib) {
        if (sib.tagName === node.tagName) index++
        sib = sib.previousElementSibling
      }
      var segment = tag + ':nth-of-type(' + index + ')'
      var cost = segment.length + (parts.length ? 3 : 0)
      if (length + cost > MAX_SELECTOR) break
      parts.unshift(segment)
      length += cost
      node = node.parentElement
      depth++
    }
    // Anchor the path at body: an unanchored '>' chain matches the same
    // tag/index shape anywhere in the document, and a body-level anchor
    // element (a selection spanning two top-level blocks) would otherwise
    // produce an empty selector.
    if (node === document.body && length + (parts.length ? 7 : 4) <= MAX_SELECTOR) {
      parts.unshift('body')
      anchored = true
    }
    return anchored || parts.length ? parts.join(' > ') : ''
  }

  // A path that provably resolves back to the element it describes, else the
  // nearest ancestor whose path does, else ''.
  //
  // cssPath is length-bounded, and once it drops outer ancestors the remaining
  // '>' chain matches the same tag/index shape ANYWHERE in the document. For a
  // text comment that was survivable — applyComments re-finds the quote inside
  // whatever it matched. For a whole-element anchor it was not: that branch
  // never calls resolveSpan, so the outline landed on the first element in
  // document order with the same inner shape, which in any repeated structure
  // (cards, table rows) is a DIFFERENT element, and the comment opened from
  // somewhere the user never clicked with nothing to detect it.
  //
  // Walking up trades precision for correctness: an ancestor's path is shorter,
  // so it fits whole and identifies one element. The quote still pins a text
  // comment inside it, and an element outline covers the right region instead
  // of the wrong element.
  function verifiedPath(el) {
    var node = el
    var hops = 0
    while (node && node.nodeType === 1 && node !== document.body && hops < 6) {
      var path = cssPath(node)
      if (path) {
        var found = null
        try { found = document.querySelector(path) } catch (err) { found = null }
        if (found === node) return path
      }
      node = node.parentElement
      hops++
    }
    return ''
  }

  // Character offset of (node, offset) within element's full text content.
  function textOffset(el, node, offset) {
    var r = document.createRange()
    r.selectNodeContents(el)
    try { r.setEnd(node, offset) } catch (e) { return null }
    return r.toString().length
  }

  function elementOf(node) {
    if (!node) return null
    return node.nodeType === 1 ? node : node.parentElement
  }

  function collapse(value) {
    return typeof value === 'string' ? value.replace(/\s+/g, ' ').trim() : ''
  }

  function selectionAnchor(sel) {
    var range = sel.getRangeAt(0)
    // The anchor element must contain BOTH endpoints. Anchoring to the start
    // container's parent breaks any selection that crosses an element
    // boundary: setEnd with a node outside that element collapses the range
    // instead of throwing, so the stored end offset is meaningless and
    // reapplication highlights only the first fragment.
    var el = elementOf(range.commonAncestorContainer)
    if (!el) return null
    var quote = clampLen(collapse(sel.toString()), MAX_QUOTE)
    if (!quote) return null
    var selector = verifiedPath(el)
    if (!selector) return null
    return {
      selector: selector,
      quote: quote,
      startOffset: textOffset(el, range.startContainer, range.startOffset),
      endOffset: textOffset(el, range.endContainer, range.endOffset),
      elementTag: el.tagName.toLowerCase()
    }
  }

  function rectsOf(range) {
    try { return range.getClientRects ? range.getClientRects() : [] } catch (e) { return [] }
  }

  // The box of the selection's END, where the cursor lands after a drag —
  // the page puts its Comment button there, as it does for Markdown.
  function endBox(range) {
    var rects = rectsOf(range)
    var r = rects.length ? rects[rects.length - 1] : range.getBoundingClientRect()
    if (!r || (!r.width && !r.height)) return null
    return {
      top: Math.round(r.top),
      left: Math.round(r.left),
      bottom: Math.round(r.bottom),
      right: Math.round(r.right)
    }
  }

  // ── Selection reporting ─────────────────────────────────────────────
  // Compose stays off until a parent proves it is listening (see Handshake in
  // artifact_bridge.py). Either message flips it, so an integrated parent
  // needs no extra call — ``ciao:apply-comments`` already proves someone is
  // on the line.
  var enabled = false
  // The selection last reported to the page ('' when none), and its anchor.
  var reportedKey = ''
  var current = null
  var selectTimer = null
  var scrollQueued = false

  function clearReported() {
    current = null
    if (!reportedKey) return
    reportedKey = ''
    post({ type: 'ciao:artifact-comment', action: 'selection-cleared' })
  }

  // Report the live selection. Each distinct selection goes out once; `moved`
  // re-sends the current one with a fresh box (the frame scrolled under it).
  function reportSelection(moved) {
    if (!enabled) return
    var sel = window.getSelection()
    if (!sel || sel.isCollapsed || sel.rangeCount === 0 || sel.toString().trim() === '') {
      clearReported()
      return
    }
    var box = endBox(sel.getRangeAt(0))
    var anchor = box ? selectionAnchor(sel) : null
    if (!anchor) {
      clearReported()
      return
    }
    var key = anchor.selector + '|' + anchor.startOffset + '|' + anchor.endOffset
    if (key === reportedKey && !moved) return
    reportedKey = key
    current = anchor
    post({
      type: 'ciao:artifact-comment',
      action: 'selection',
      selector: anchor.selector,
      quote: anchor.quote,
      startOffset: anchor.startOffset,
      endOffset: anchor.endOffset,
      elementTag: anchor.elementTag,
      rect: box
    })
  }

  // Settle a pending selectionchange now. A keystroke can follow a new
  // selection inside the debounce, and must not open a comment on the old one.
  function flushSelection() {
    if (!selectTimer) return
    clearTimeout(selectTimer)
    selectTimer = null
    reportSelection(false)
  }

  document.addEventListener('selectionchange', function () {
    if (!enabled) return
    if (selectTimer) clearTimeout(selectTimer)
    selectTimer = setTimeout(function () {
      selectTimer = null
      reportSelection(false)
    }, 150)
  })

  // Capture, so a scroll inside any of the artifact's own scrollers counts.
  window.addEventListener('scroll', function () {
    if (!reportedKey || scrollQueued) return
    scrollQueued = true
    var run = function () {
      scrollQueued = false
      if (reportedKey) reportSelection(true)
    }
    if (window.requestAnimationFrame) window.requestAnimationFrame(run)
    else setTimeout(run, 16)
  }, true)

  // ── Type-to-comment ─────────────────────────────────────────────────
  // The page's type-to-comment listens on the page's document; keys typed
  // with focus in this frame never reach it. With a reported selection, the
  // first printable key or a paste is forwarded instead, and swallowed so it
  // does not also act on the artifact.
  function isEditable(target) {
    var el = elementOf(target)
    if (!el) return false
    if (el.isContentEditable) return true
    return !!(el.closest && el.closest('input, textarea, select, [contenteditable=""], [contenteditable="true"]'))
  }

  document.addEventListener('keydown', function (e) {
    if (!enabled || isEditable(e.target)) return
    // Same rule as the page's isTypeThrough: one printable character, no
    // modifier (Cmd/Ctrl+V arrives as a paste), not space, not mid-IME.
    if (e.metaKey || e.ctrlKey || e.altKey || e.isComposing) return
    var key = e.key
    if (typeof key !== 'string' || key === ' ' || !ONE_CHAR.test(key)) return
    flushSelection()
    if (!current) return
    e.preventDefault()
    e.stopPropagation()
    post({ type: 'ciao:artifact-comment', action: 'type', text: key })
  }, true)

  document.addEventListener('paste', function (e) {
    if (!enabled || isEditable(e.target)) return
    flushSelection()
    if (!current) return
    var data = e.clipboardData
    if (!data) return
    var text = data.getData('text/plain') || ''
    // Files cross postMessage by structured clone, so pasted screenshots can
    // ride along to the composer the way they do on the page.
    var files = []
    var list = data.files || []
    for (var i = 0; i < list.length; i++) {
      if (list[i] && /^image\//.test(list[i].type || '')) files.push(list[i])
    }
    if (!text.trim() && !files.length) return
    e.preventDefault()
    e.stopPropagation()
    var message = { type: 'ciao:artifact-comment', action: 'type', text: text }
    if (files.length) message.files = files
    post(message)
  }, true)

  // ── Clicks ──────────────────────────────────────────────────────────
  // Alt+Click comments the element itself (for non-text things: a chart node,
  // an SVG shape, a table cell the user cannot easily select). A plain click
  // on a highlight opens its comment.
  document.addEventListener('click', function (e) {
    if (!enabled) return
    if (e.altKey) {
      var el = elementOf(e.target)
      if (!el || el === document.body) return
      var selector = verifiedPath(el)
      if (!selector) return
      e.preventDefault()
      e.stopPropagation()
      var rect = el.getBoundingClientRect()
      // The quote is empty for a textless node — that is the case this path
      // exists for. Reapplication outlines the element, so an empty quote
      // still gets a visible, clickable anchor.
      post({
        type: 'ciao:artifact-comment',
        action: 'compose',
        selector: selector,
        quote: clampLen(collapse(el.textContent), 200),
        startOffset: 0,
        endOffset: el.textContent ? el.textContent.length : 0,
        elementTag: el.tagName.toLowerCase(),
        wholeElement: true,
        x: Math.round(rect.left),
        y: Math.round(rect.bottom)
      })
      return
    }
    var anchor = e.target && e.target.closest
      ? e.target.closest('mark.' + MARK_CLASS + ', .' + EL_CLASS)
      : null
    var id = ''
    var box = null
    if (anchor) {
      id = nextId(anchor)
      box = anchor.getBoundingClientRect()
    } else {
      // A drag that ends over a highlight is a selection, not a click on it.
      var sel = window.getSelection()
      if (sel && !sel.isCollapsed) return
      var hit = hitTest(e.clientX, e.clientY)
      if (hit) {
        id = hit.id
        box = hit.rect
      }
    }
    if (!id) return
    // Consume it. A highlight can sit inside a link or a button, and letting
    // the click through would navigate the frame (or mutate the artifact)
    // out from under the comment the user was opening.
    e.preventDefault()
    e.stopPropagation()
    post({
      type: 'ciao:artifact-comment',
      action: 'open',
      id: id,
      x: Math.round(box.left),
      y: Math.round(box.bottom)
    })
  }, true)

  // Which comment a click on this anchor opens. An anchor usually holds one
  // id, but several whole-element comments can share one element, so the ids
  // are a list and repeated clicks walk it — otherwise every comment but the
  // last would have no clickable anchor at all.
  function nextId(anchor) {
    var ids = idsOf(anchor)
    if (!ids.length) return ''
    var cursor = parseInt(anchor.getAttribute(CURSOR_ATTR) || '0', 10)
    if (!(cursor >= 0) || cursor >= ids.length) cursor = 0
    anchor.setAttribute(CURSOR_ATTR, String((cursor + 1) % ids.length))
    return ids[cursor]
  }

  function idsOf(el) {
    var raw = (el.getAttribute(ID_ATTR) || '').split(/\s+/)
    var out = []
    for (var i = 0; i < raw.length; i++) {
      if (raw[i]) out.push(raw[i])
    }
    return out
  }

  // Painted text ranges ({ id, range }) — not elements, so a click finds them
  // by geometry. Overlapping comments all hit; repeated clicks on the same
  // spot walk them, like nextId does for an element.
  var painted = []
  var hitCursor = { key: '', next: 0 }

  function hitTest(x, y) {
    if (typeof x !== 'number' || typeof y !== 'number') return null
    var hits = []
    for (var i = 0; i < painted.length; i++) {
      var rects = rectsOf(painted[i].range)
      for (var j = 0; j < rects.length; j++) {
        var r = rects[j]
        if (x >= r.left && x <= r.right && y >= r.top && y <= r.bottom) {
          hits.push({ id: painted[i].id, rect: r })
          break
        }
      }
    }
    if (!hits.length) return null
    var ids = []
    for (var k = 0; k < hits.length; k++) ids.push(hits[k].id)
    var key = ids.join(' ')
    if (key !== hitCursor.key) hitCursor = { key: key, next: 0 }
    var pick = hits[hitCursor.next % hits.length]
    hitCursor.next = (hitCursor.next + 1) % hits.length
    return pick
  }

  // ── Highlight (re)application ───────────────────────────────────────
  function setClass(el, name, on) {
    if (el.classList) {
      if (on) el.classList.add(name)
      else el.classList.remove(name)
      return
    }
    // SVG elements in old webviews: className is an SVGAnimatedString, so the
    // attribute is the only writable path.
    var current = (el.getAttribute('class') || '').split(/\s+/)
    var kept = []
    for (var i = 0; i < current.length; i++) {
      if (current[i] && current[i] !== name) kept.push(current[i])
    }
    if (on) kept.push(name)
    el.setAttribute('class', kept.join(' '))
  }

  function clearMarks() {
    painted = []
    if (hasHighlightApi()) window.CSS.highlights['delete'](HIGHLIGHT_NAME)
    var marks = document.querySelectorAll('mark.' + MARK_CLASS)
    for (var i = 0; i < marks.length; i++) {
      var m = marks[i]
      var parent = m.parentNode
      if (!parent) continue
      parent.replaceChild(document.createTextNode(m.textContent || ''), m)
      parent.normalize()
    }
    var outlined = document.querySelectorAll('.' + EL_CLASS)
    for (var j = 0; j < outlined.length; j++) {
      setClass(outlined[j], EL_CLASS, false)
      outlined[j].removeAttribute(ID_ATTR)
      outlined[j].removeAttribute(CURSOR_ATTR)
    }
  }

  // A Range over [start, end) of el's text content, or null.
  function rangeFor(el, start, end) {
    var walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, null)
    var range = document.createRange()
    var pos = 0
    var started = false
    var node
    while ((node = walker.nextNode())) {
      var len = node.nodeValue ? node.nodeValue.length : 0
      if (!started && pos + len > start) {
        range.setStart(node, start - pos)
        started = true
      }
      if (started && pos + len >= end) {
        range.setEnd(node, end - pos)
        return range
      }
      pos += len
    }
    return null
  }

  // Whether the <mark> fallback may wrap this text node. Whitespace between
  // blocks has nothing to show, and a text node that is table structure (the
  // indentation between <th>s, say) turns its <mark> into a phantom cell.
  function markable(node) {
    if (!node.nodeValue || !/\S/.test(node.nodeValue)) return false
    var parent = node.parentNode
    return !(parent && parent.nodeType === 1 && TABLE_STRUCTURE.test(parent.nodeName.toUpperCase()))
  }

  // Fallback only: wrap [start, end) of el's text content in one mark per
  // markable text node touched.
  function wrapRange(el, start, end, id) {
    var walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, null)
    var pos = 0
    var node
    var touched = []
    while ((node = walker.nextNode())) {
      var len = node.nodeValue ? node.nodeValue.length : 0
      var nodeEnd = pos + len
      if (nodeEnd > start && pos < end && markable(node)) touched.push({ node: node, pos: pos, len: len })
      pos = nodeEnd
      if (pos >= end) break
    }
    for (var i = touched.length - 1; i >= 0; i--) {
      var t = touched[i]
      var text = t.node.nodeValue || ''
      var from = Math.max(0, start - t.pos)
      var to = Math.min(text.length, end - t.pos)
      if (to <= from) continue
      var mark = document.createElement('mark')
      mark.className = MARK_CLASS
      mark.setAttribute(ID_ATTR, id)
      var mid = document.createTextNode(text.slice(from, to))
      mark.appendChild(mid)
      var parent = t.node.parentNode
      if (!parent) continue
      parent.replaceChild(mark, t.node)
      if (from > 0) parent.insertBefore(document.createTextNode(text.slice(0, from)), mark)
      if (to < text.length) parent.insertBefore(document.createTextNode(text.slice(to)), mark.nextSibling)
    }
  }

  // Locate the quote in el's raw text content. Quotes are stored
  // whitespace-collapsed, but textContent keeps the document's newlines and
  // indentation, so a plain indexOf misses every multi-line selection.
  function findQuote(text, quote) {
    var at = text.indexOf(quote)
    if (at >= 0) return { start: at, end: at + quote.length }
    var pattern = quote.replace(/[.*+?^${}()|[\]\\]/g, '\\$&').replace(/ /g, '\\s+')
    var m = null
    try { m = new RegExp(pattern).exec(text) } catch (e) { m = null }
    if (!m) return null
    return { start: m.index, end: m.index + m[0].length }
  }

  // Prefer the stored offsets, but only when the text still sitting there is
  // the quoted text — otherwise the document moved under the anchor and the
  // quote search is the better guess.
  function resolveSpan(text, c) {
    var start = typeof c.startOffset === 'number' ? c.startOffset : -1
    var end = typeof c.endOffset === 'number' ? c.endOffset : -1
    if (start >= 0 && end > start && start < text.length) {
      var stop = Math.min(end, text.length)
      var here = collapse(text.slice(start, stop))
      var quote = collapse(c.quote)
      // A selection longer than MAX_QUOTE is stored quote-truncated while the
      // offsets still span the whole thing, so the equality below can never
      // hold for it and it fell through to `findQuote` — which matches the
      // 500-char prefix and highlighted under half of what the user picked.
      // A quote sitting at the cap is the truncated case; accept the offsets
      // when the stored prefix is still what is there.
      //
      // Measured on the STORED quote, before collapse(): the cap slice can
      // land on a space and collapse() trims, so a truncated 500-char quote
      // comes back 499 here and this test missed it — with roughly one space
      // per five or six characters of prose, about one long selection in six
      // still highlighted only its stored prefix.
      var stored = typeof c.quote === 'string' ? c.quote : ''
      var truncated = stored.length >= MAX_QUOTE
      if (here === quote || (truncated && here.indexOf(quote) === 0)) {
        return { start: start, end: stop }
      }
    }
    return findQuote(text, c.quote)
  }

  function applyComments(comments) {
    clearMarks()
    if (!comments || !comments.length) return
    var api = hasHighlightApi()
    var highlight = api ? new window.Highlight() : null
    for (var i = 0; i < comments.length; i++) {
      var c = comments[i]
      if (!c || !c.selector) continue
      var el = null
      try { el = document.querySelector(c.selector) } catch (e) { el = null }
      if (!el) continue
      // Whole-element anchors outline the element rather than painting text:
      // the case they exist for (an SVG shape, a chart bar) has no text to
      // paint, and an empty quote must still leave something clickable.
      if (c.wholeElement) {
        setClass(el, EL_CLASS, true)
        // Append: two comments can anchor to the same element, and replacing
        // the attribute left the first one with nothing to click.
        var have = idsOf(el)
        have.push(String(c.id))
        el.setAttribute(ID_ATTR, have.join(' '))
        el.removeAttribute(CURSOR_ATTR)
        continue
      }
      if (!c.quote) continue
      var span = resolveSpan(el.textContent || '', c)
      if (!span || span.end <= span.start) continue
      if (api) {
        var range = rangeFor(el, span.start, span.end)
        if (!range) continue
        highlight.add(range)
        painted.push({ id: String(c.id), range: range })
      } else {
        wrapRange(el, span.start, span.end, String(c.id))
      }
    }
    if (api && painted.length) window.CSS.highlights.set(HIGHLIGHT_NAME, highlight)
  }

  window.addEventListener('message', function (e) {
    var data = e.data
    if (!data || data.frame !== 'ciao-artifact') return
    if (data.type === 'ciao:comments-enable') { enabled = true; return }
    if (data.type === 'ciao:comments-disable') {
      clearReported()
      enabled = false
      return
    }
    if (data.type === 'ciao:clear-selection') {
      // The page opened its composer on the reported selection; drop the
      // native one so the draft highlight shows and no stale report lingers.
      if (selectTimer) { clearTimeout(selectTimer); selectTimer = null }
      reportedKey = ''
      current = null
      var sel = window.getSelection()
      if (sel) sel.removeAllRanges()
      return
    }
    if (data.type !== 'ciao:apply-comments') return
    enabled = true
    applyComments(data.comments)
  })

  // The bridge is injected into <head>, so the document is still parsing here.
  // Announcing readiness now would have the parent push its highlights at a
  // tree with no <body> yet, and every selector would miss. Wait for the DOM.
  function announceReady() {
    post({ type: 'ciao:artifact-comment', action: 'ready' })
  }
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', announceReady)
  } else {
    announceReady()
  }
})()"""

# Real newlines around the script (a plain f-string, so ``\n`` is a newline and
# not a literal backslash): without them a trailing ``//`` line comment in the
# script would swallow the closing tag.
BRIDGE_TAG = f"<script {_BRIDGE_MARKER}>\n{BRIDGE_SCRIPT}\n</script>"


def inject_bridge(html: str) -> str:
    """Return ``html`` with the bridge script injected.

    Preferred spot is right after the opening ``<head>`` tag, so the bridge's
    style element lands before the artifact paints. Head-less documents fall
    back to ``<body>``, then ``<html>``, then just after the doctype.

    Never prepend to a document that starts with a doctype: a ``<script>``
    ahead of the doctype makes the parser ignore it and render the artifact in
    quirks mode — a different box model, ``line-height``, and percentage
    heights than the same file got before injection existed. Bare fragments
    (no doctype, no structural tags) are still prepended, which is what they
    want and costs nothing.

    An artifact that somehow already carries the marker is returned unchanged.
    """
    if _BRIDGE_MARKER in html:
        return html
    for pattern in (_HEAD_RE, _BODY_RE, _HTML_RE, _DOCTYPE_RE):
        match = pattern.search(html)
        if match:
            idx = match.end()
            return html[:idx] + BRIDGE_TAG + html[idx:]
    return BRIDGE_TAG + html