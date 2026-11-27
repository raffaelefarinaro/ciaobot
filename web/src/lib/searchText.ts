// Pure helpers for the chat search dialog: turn a markdown excerpt into plain
// text and split text around the query terms so the view can wrap the matches
// in <mark> by interpolation, never by v-html.

export type TextSegment = { text: string; match: boolean }

/** Flatten a markdown excerpt to one line of plain text. */
export function plainText(markdown: string): string {
  return markdown
    .replace(/```[a-zA-Z0-9_-]*/g, ' ')
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/^[ \t]*(?:#{1,6}|>+|[-*+]|\d+[.)])[ \t]+/gm, '')
    .replace(/(\*\*|__|~~|`)/g, '')
    .replace(/\s+/g, ' ')
    .trim()
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/** Split text into runs, flagging the runs that equal a query term (case-insensitive). */
export function highlightSegments(text: string, query: string): TextSegment[] {
  const terms = [...new Set(query.trim().split(/\s+/).filter(Boolean))].sort((a, b) => b.length - a.length)
  if (!terms.length || !text) return [{ text, match: false }]
  const pattern = new RegExp(`(${terms.map(escapeRegExp).join('|')})`, 'gi')
  const segments: TextSegment[] = []
  let last = 0
  for (const found of text.matchAll(pattern)) {
    const start = found.index ?? 0
    if (start > last) segments.push({ text: text.slice(last, start), match: false })
    segments.push({ text: found[0], match: true })
    last = start + found[0].length
  }
  if (last < text.length) segments.push({ text: text.slice(last), match: false })
  return segments
}
