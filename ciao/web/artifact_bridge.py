"""In-frame comment bridge for HTML artifact previews.

An artifact renders in a sandboxed iframe (opaque origin, ``allow-scripts``,
no same-origin access). The PWA parent cannot reach into that document, and
the document cannot reach the parent — except through ``postMessage``, which
works across origins in both directions.

This module holds the small script injected into every artifact response by
``workspace_html`` (see ``inject_bridge``). The script runs inside the frame
and provides the artifact-side half of selection comments:

- It watches ``selectionchange`` and floats a Comment pill near the selection,
  but only once the parent has said it is listening (see the handshake below).
- Clicking the pill (or Alt-clicking any element to comment on the element
  rather than its text) builds an anchor — a CSS selector path plus character
  offsets into that element's text — and posts it to the parent window.
- It re-applies durable comment highlights on request: the parent sends a
  ``ciao:apply-comments`` message after the frame loads or the comment list
  changes, and the script wraps the anchored text in <mark> elements.

Handshake: the compose half is inert until the parent posts
``ciao:comments-enable`` (or any ``ciao:apply-comments``, which proves the same
thing). The PWA side of selection comments does not exist yet, and shipping a
Comment pill whose messages nothing receives is worse than shipping no pill —
it reads as a working feature. Highlight re-application is unaffected: it only
ever acts on comments the parent itself sent.

Trust model: the script is our code, but it shares a document with
model-authored script, which could forge the same messages. That is
acceptable — the worst case is a pending comment appearing in the composer,
which the user reviews before sending. The bridge makes no network requests
(impossible under the artifact CSP anyway) and touches nothing but the
highlight marks it owns.
"""

from __future__ import annotations

import re

# Marker attribute on the injected script tag; also the idempotency check.
_BRIDGE_MARKER = "data-ciao-artifact-bridge"

_HEAD_RE = re.compile(r"<head\b[^>]*>", re.IGNORECASE)

# Plain ES5: the frame is a WKWebView/Chromium webview, but nothing here needs
# anything newer, and older webviews in the desktop shell are a real audience.
BRIDGE_SCRIPT = """(function () {
  if (window.__ciaoArtifactBridge) return
  window.__ciaoArtifactBridge = true

  var MARK_CLASS = 'ciao-comment-mark'
  var PILL_ID = 'ciao-comment-pill'
  var MAX_QUOTE = 500
  var MAX_SELECTOR = 400

  var css = document.createElement('style')
  css.setAttribute('data-ciao-bridge-style', '')
  css.textContent =
    'mark.' + MARK_CLASS + '{background:rgba(255,77,109,.28);color:inherit;' +
    'border-radius:2px;cursor:pointer;box-decoration-break:clone;-webkit-box-decoration-break:clone}' +
    '#' + PILL_ID + '{position:absolute;z-index:2147483647;background:#ff4d6d;color:#fff;' +
    'font:600 12px/1 system-ui,-apple-system,sans-serif;padding:6px 11px;border-radius:999px;' +
    'border:0;cursor:pointer;box-shadow:0 2px 10px rgba(0,0,0,.35)}'
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
  function cssPath(el) {
    var parts = []
    var node = el
    var depth = 0
    while (node && node.nodeType === 1 && node !== document.body && depth < 16) {
      var tag = node.tagName.toLowerCase()
      var index = 1
      var sib = node.previousElementSibling
      while (sib) {
        if (sib.tagName === node.tagName) index++
        sib = sib.previousElementSibling
      }
      parts.unshift(tag + ':nth-of-type(' + index + ')')
      node = node.parentElement
      depth++
    }
    return parts.join(' > ')
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

  function buildSelectionPayload(sel, rect) {
    var range = sel.getRangeAt(0)
    var el = elementOf(range.startContainer)
    if (!el || el === document.body) return null
    var quote = clampLen(sel.toString().trim().replace(/\\s+/g, ' '), MAX_QUOTE)
    if (!quote) return null
    return {
      type: 'ciao:artifact-comment',
      action: 'compose',
      selector: clampLen(cssPath(el), MAX_SELECTOR),
      quote: quote,
      startOffset: textOffset(el, range.startContainer, range.startOffset),
      endOffset: textOffset(el, range.endContainer, range.endOffset),
      elementTag: el.tagName.toLowerCase(),
      x: Math.round(rect.left),
      y: Math.round(rect.bottom)
    }
  }

  // ── Comment pill ────────────────────────────────────────────────────
  // The compose affordances stay inert until the parent says it is listening.
  // The PWA half does not exist yet, and a Comment pill that posts into the
  // void is worse than no pill: it looks like a feature and does nothing.
  // Either handshake message enables them, so an integrated parent needs no
  // extra call — `ciao:apply-comments` already proves someone is on the line.
  var enabled = false
  var pill = null
  var pillTimer = null

  function removePill() {
    if (pill) { pill.remove(); pill = null }
  }

  function showPill(sel) {
    removePill()
    var range = sel.getRangeAt(0)
    var rect = range.getBoundingClientRect()
    if (!rect || (!rect.width && !rect.height)) return
    var payload = buildSelectionPayload(sel, rect)
    if (!payload) return
    pill = document.createElement('button')
    pill.id = PILL_ID
    pill.type = 'button'
    pill.textContent = 'Comment'
    pill.addEventListener('mousedown', function (e) { e.preventDefault() })
    pill.addEventListener('click', function (e) {
      e.stopPropagation()
      removePill()
      sel.removeAllRanges()
      post(payload)
    })
    document.body.appendChild(pill)
    var x = window.scrollX + rect.right + 8
    var y = window.scrollY + rect.bottom + 6
    if (x + 110 > window.scrollX + document.documentElement.clientWidth) {
      x = window.scrollX + rect.left
    }
    pill.style.left = x + 'px'
    pill.style.top = y + 'px'
  }

  document.addEventListener('selectionchange', function () {
    if (!enabled) return
    if (pillTimer) clearTimeout(pillTimer)
    pillTimer = setTimeout(function () {
      pillTimer = null
      var sel = window.getSelection()
      if (!sel || sel.isCollapsed || sel.rangeCount === 0 || sel.toString().trim() === '') {
        removePill()
        return
      }
      showPill(sel)
    }, 150)
  })

  // Alt+Click comments the element itself (for non-text things: a chart node,
  // an SVG shape, a table cell the user cannot easily select).
  document.addEventListener('click', function (e) {
    if (!enabled) return
    var mark = e.target && e.target.closest ? e.target.closest('mark.' + MARK_CLASS) : null
    if (e.altKey) {
      var el = elementOf(e.target)
      if (!el || el === document.body) return
      e.preventDefault()
      e.stopPropagation()
      var rect = el.getBoundingClientRect()
      post({
        type: 'ciao:artifact-comment',
        action: 'compose',
        selector: clampLen(cssPath(el), MAX_SELECTOR),
        quote: clampLen((el.textContent || '').trim().replace(/\\s+/g, ' '), 200),
        startOffset: 0,
        endOffset: el.textContent ? el.textContent.length : 0,
        elementTag: el.tagName.toLowerCase(),
        wholeElement: true,
        x: Math.round(rect.left),
        y: Math.round(rect.bottom)
      })
      return
    }
    if (mark) {
      var id = mark.getAttribute('data-ciao-comment-id')
      if (!id) return
      var r = mark.getBoundingClientRect()
      post({
        type: 'ciao:artifact-comment',
        action: 'open',
        id: id,
        x: Math.round(r.left),
        y: Math.round(r.bottom)
      })
    }
  }, true)

  // ── Highlight (re)application ───────────────────────────────────────
  function clearMarks() {
    var marks = document.querySelectorAll('mark.' + MARK_CLASS)
    for (var i = 0; i < marks.length; i++) {
      var m = marks[i]
      var parent = m.parentNode
      if (!parent) continue
      parent.replaceChild(document.createTextNode(m.textContent || ''), m)
      parent.normalize()
    }
  }

  // Wrap [start, end) of el's text content in one mark per text node touched.
  function wrapRange(el, start, end, id) {
    var walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, null)
    var pos = 0
    var node
    var touched = []
    while ((node = walker.nextNode())) {
      var len = node.nodeValue ? node.nodeValue.length : 0
      var nodeEnd = pos + len
      if (nodeEnd > start && pos < end) touched.push({ node: node, pos: pos, len: len })
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
      mark.setAttribute('data-ciao-comment-id', id)
      var mid = document.createTextNode(text.slice(from, to))
      mark.appendChild(mid)
      var parent = t.node.parentNode
      if (!parent) continue
      parent.replaceChild(mark, t.node)
      if (from > 0) parent.insertBefore(document.createTextNode(text.slice(0, from)), mark)
      if (to < text.length) parent.insertBefore(document.createTextNode(text.slice(to)), mark.nextSibling)
    }
  }

  function applyComments(comments) {
    clearMarks()
    if (!comments || !comments.length) return
    for (var i = 0; i < comments.length; i++) {
      var c = comments[i]
      if (!c || !c.selector || !c.quote) continue
      var el = null
      try { el = document.querySelector(c.selector) } catch (e) { el = null }
      if (!el) continue
      var text = el.textContent || ''
      var start = typeof c.startOffset === 'number' ? c.startOffset : text.indexOf(c.quote)
      if (typeof start !== 'number' || start < 0) start = text.indexOf(c.quote)
      if (start < 0) continue
      var end = typeof c.endOffset === 'number' && c.endOffset > start
        ? Math.min(c.endOffset, text.length)
        : Math.min(start + c.quote.length, text.length)
      if (c.wholeElement) { start = 0; end = text.length }
      if (end <= start) continue
      wrapRange(el, start, end, String(c.id))
    }
  }

  window.addEventListener('message', function (e) {
    var data = e.data
    if (!data || data.frame !== 'ciao-artifact') return
    if (data.type === 'ciao:comments-enable') { enabled = true; return }
    if (data.type === 'ciao:comments-disable') { enabled = false; removePill(); return }
    if (data.type !== 'ciao:apply-comments') return
    enabled = true
    applyComments(data.comments)
  })

  post({ type: 'ciao:artifact-comment', action: 'ready' })
})()"""

BRIDGE_TAG = f"<script {_BRIDGE_MARKER}>\n{BRIDGE_SCRIPT}\n</script>"


def inject_bridge(html: str) -> str:
    """Return ``html`` with the bridge script injected.

    Inserted right after the opening ``<head>`` tag when one exists so the
    bridge's style element lands before the artifact paints; otherwise the
    document is prepended, which is valid for fragments without a head.
    An artifact that somehow already carries the marker is returned unchanged.
    """
    if _BRIDGE_MARKER in html:
        return html
    match = _HEAD_RE.search(html)
    if match:
        idx = match.end()
        return html[:idx] + BRIDGE_TAG + html[idx:]
    return BRIDGE_TAG + html