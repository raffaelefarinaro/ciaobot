/**
 * Text matching for comment anchors.
 *
 * Browser selections and rendered markdown do not always expose identical
 * strings. Block boundaries can become missing newlines in textContent, and
 * Unicode format characters can be present on one side but not the other.
 * Keep the original offsets so callers can still wrap the exact rendered text.
 */

// Match-time normalization ignores every Unicode format character. Selection
// cleanup is narrower so meaningful emoji joiners stay in the quoted text.
const MATCH_FORMAT_CHAR_RE = /\p{Cf}/gu
const CLEAN_SELECTION_FORMAT_CHAR_RE = /[\u00ad\u061c\u180e\u200b\u200e\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]/gu

type NormalizedText = {
  text: string
  starts: number[]
  ends: number[]
}

function normalizedText(value: string): NormalizedText {
  let text = ''
  const starts: number[] = []
  const ends: number[] = []

  for (let rawIndex = 0; rawIndex < value.length;) {
    const codePoint = value.codePointAt(rawIndex)
    if (codePoint == null) break
    const rawEnd = rawIndex + (codePoint > 0xffff ? 2 : 1)
    const rawChar = value.slice(rawIndex, rawEnd)
    const normalized = rawChar.normalize('NFKC').replace(MATCH_FORMAT_CHAR_RE, '')

    for (let normalizedIndex = 0; normalizedIndex < normalized.length;) {
      const normalizedCodePoint = normalized.codePointAt(normalizedIndex)
      if (normalizedCodePoint == null) break
      const normalizedEnd = normalizedIndex + (normalizedCodePoint > 0xffff ? 2 : 1)
      const normalizedChar = normalized.slice(normalizedIndex, normalizedEnd)

      if (/\s/u.test(normalizedChar)) {
        // Rendered markdown can omit the newline between block elements, while
        // Selection.toString() can include one. Treat every whitespace run as
        // one searchable space and extend its raw range as it collapses.
        if (text.endsWith(' ')) {
          ends[ends.length - 1] = rawEnd
        } else {
          text += ' '
          starts.push(rawIndex)
          ends.push(rawEnd)
        }
      } else {
        for (let i = 0; i < normalizedChar.length; i++) {
          text += normalizedChar[i]
          starts.push(rawIndex)
          ends.push(rawEnd)
        }
      }
      normalizedIndex = normalizedEnd
    }

    rawIndex = rawEnd
  }

  return { text, starts, ends }
}

function normalizedNeedle(value: string): string {
  return normalizedText(value).text.trim()
}

function nthIndexOf(value: string, needle: string, occurrenceIndex: number): number {
  const target = Math.max(0, occurrenceIndex)
  let from = 0
  let occurrence = 0
  while (from <= value.length - needle.length) {
    const index = value.indexOf(needle, from)
    if (index === -1) break
    if (occurrence === target) return index
    occurrence++
    from = index + 1
  }
  return value.indexOf(needle)
}

function compactIndex(indexed: NormalizedText): NormalizedText {
  let text = ''
  const starts: number[] = []
  const ends: number[] = []
  for (let i = 0; i < indexed.text.length; i++) {
    if (indexed.text[i] === ' ') continue
    text += indexed.text[i]
    starts.push(indexed.starts[i])
    ends.push(indexed.ends[i])
  }
  return { text, starts, ends }
}

/** Remove invisible Unicode formatting characters before storing a quote. */
export function cleanCommentSelection(value: string): string {
  return value.normalize('NFKC').replace(CLEAN_SELECTION_FORMAT_CHAR_RE, '')
}

/**
 * Escape a value for use inside a quoted CSS attribute-selector string.
 * Backslash and quote must be escaped in one pass: escaping only `"` lets a
 * value ending in `\` turn the inserted `\"` into an escaped backslash plus
 * a string-terminating quote, breaking out of the selector.
 */
export function escapeCssAttrValue(value: string): string {
  return value.replace(/[\\"]/g, '\\$&')
}

/**
 * Find a comment quote in rendered text and map the match back to raw offsets.
 * The fallback matching is Unicode-normalized and whitespace-tolerant, while
 * the returned offsets always refer to the original rendered string.
 */
export function findCommentTextMatch(
  source: string,
  selection: string,
  occurrenceIndex = 0,
): { start: number; end: number } | null {
  const needle = normalizedNeedle(selection)
  if (!needle) return null

  let indexed = normalizedText(source)
  let indexedNeedle = needle
  let start = nthIndexOf(indexed.text, indexedNeedle, occurrenceIndex)
  if (start === -1) {
    // textContent omits the separator between adjacent block elements, while
    // Selection.toString() includes a line break. Compact matching covers that
    // renderer/browser difference without losing raw offset mapping.
    indexed = compactIndex(indexed)
    indexedNeedle = needle.replace(/ /g, '')
    start = nthIndexOf(indexed.text, indexedNeedle, occurrenceIndex)
  }
  if (start === -1) return null

  const endIndex = start + indexedNeedle.length - 1
  const rawStart = indexed.starts[start]
  const rawEnd = indexed.ends[endIndex]
  if (rawStart == null || rawEnd == null) return null
  return { start: rawStart, end: rawEnd }
}

/** Return whether a stored quote can be located in rendered text. */
export function commentTextMatches(source: string, selection: string): boolean {
  return findCommentTextMatch(source, selection) !== null
}

/**
 * Count matching occurrences before a live selection start. This keeps the
 * duplicate-quote anchor stable even when the quote contains format chars or
 * crosses rendered whitespace boundaries.
 */
export function commentTextOccurrenceIndex(
  source: string,
  selection: string,
  rawStart: number,
): number {
  const needle = normalizedNeedle(selection)
  if (!needle) return 0

  const indexed = normalizedText(source)
  const normalizedStart = indexed.starts.findIndex(index => index >= rawStart)
  const limit = normalizedStart === -1 ? indexed.text.length : normalizedStart
  let occurrence = 0
  let from = 0
  while (from <= limit - needle.length) {
    const index = indexed.text.indexOf(needle, from)
    if (index === -1 || index >= limit) break
    occurrence++
    from = index + 1
  }
  return occurrence
}

/** Wrap the matched rendered text without requiring one Range per DOM node. */
export function highlightCommentText(
  root: HTMLElement,
  selection: string,
  commentId: string,
  occurrenceIndex?: number,
): boolean {
  const text = selection.trim()
  if (!text) return false
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT)
  const nodes: Text[] = []
  let node: Node | null
  while ((node = walker.nextNode())) nodes.push(node as Text)
  if (!nodes.length) return false

  let fullText = ''
  const offsets: { node: Text; start: number; end: number }[] = []
  for (const n of nodes) {
    const start = fullText.length
    fullText += n.textContent || ''
    offsets.push({ node: n, start, end: fullText.length })
  }

  const match = findCommentTextMatch(fullText, text, occurrenceIndex)
  if (!match) return false

  let success = false
  for (let i = offsets.length - 1; i >= 0; i--) {
    const o = offsets[i]
    if (o.end <= match.start || o.start >= match.end) continue
    const localStart = Math.max(0, match.start - o.start)
    const localEnd = Math.min(o.end - o.start, match.end - o.start)
    if (localStart >= localEnd) continue

    const textNode = o.node
    const slice = textNode.textContent?.slice(localStart, localEnd) || ''
    if (!slice.trim()) continue

    try {
      textNode.splitText(localEnd)
      const mid = textNode.splitText(localStart)
      const span = document.createElement('span')
      span.className = 'comment-highlight'
      span.dataset.commentId = commentId
      mid.parentNode?.replaceChild(span, mid)
      span.appendChild(mid)
      success = true
    } catch {
      // A stale or disconnected text node should not block other nodes.
    }
  }
  return success
}
