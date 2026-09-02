import { excelColLetter } from './csv'
import { formatArtifactCommentLocation } from './artifactBridge'

// Formatting for "comment" context — text a user selects (in a chat reply or a
// document preview) plus the note they attach — that rides along with their
// next prompt to the agent.
//
// The same string is (a) sent verbatim to the model over the chat WebSocket and
// (b) rendered back into the user's own chat bubble as markdown. So the format
// has to be a strong, unambiguous boundary for the model AND render cleanly for
// the human.
//
// We use named XML-style tags rather than bare quotes or markdown blockquotes.
// This follows Anthropic's prompting guidance (Claude is trained heavily on XML
// tags, so `<quoted-text>` reads as an unambiguous container where a `>` or a
// `"..."` is only a soft visual cue with no closing delimiter). The tags are
// whitelisted in the renderer (see lib/safeMarkdown.ts) and styled as quote
// cards in the chat bubble (see ChatPanel.vue), so they survive into the UI
// instead of being stripped.
//
// The selection is untrusted verbatim text and may itself contain any of these
// delimiters; neutralizeTags() defuses that so a selection can't forge or break
// a boundary the model (or the HTML parser) relies on.

export interface FileCommentInput {
  path: string
  selection: string
  comment: string
  lineStart?: number | null
  lineEnd?: number | null
  /** 0-indexed CSV column; when set, source uses a cell locator instead of line. */
  colIndex?: number | null
  /** CSV column header label (preferred over "Column N" in the locator). */
  colHeader?: string | null
  /**
   * HTML artifact anchor: CSS selector + text offsets inside the rendered
   * page. When set, source uses an element locator instead of line numbers.
   */
  artifactSelector?: string | null
  artifactStartOffset?: number | null
  artifactEndOffset?: number | null
  artifactElementTag?: string | null
  artifactWholeElement?: boolean
  images?: string[]
}

/**
 * Where in the transcript a chat comment's quoted text came from.
 *
 * All optional: a comment made before this was plumbed through, or restored
 * from an older persisted bucket, carries none of it. Declared once here
 * because the store, the draft state, and this formatter all need the same
 * shape — see `formatChatReferenceSource`.
 */
export interface ChatCommentAnchor {
  messageId?: string
  messageIndex?: number
  messageRole?: string
  occurrenceIndex?: number
  paragraphIndex?: number
}

export interface ChatCommentInput extends ChatCommentAnchor {
  selection: string
  comment: string
  images?: string[]
}

// The custom elements we emit. Exported so the renderer allow-list and the CSS
// stay in sync with this one source of truth.
export const COMMENT_TAGS = [
  'user-comment-reference',
  'reference-source',
  'quoted-text',
  'user-comment',
] as const

const TAG_PATTERN = new RegExp(`<(/?)(${COMMENT_TAGS.join('|')})>`, 'gi')

// Insert a zero-width space after the `<` of any of our own tags that appears
// inside untrusted content. Invisible in the bubble, but the result is no
// longer a real tag, so an embedded delimiter can't close a container early or
// fake a new one.
function neutralizeTags(value: string): string {
  return value.replace(TAG_PATTERN, '<\u200b$1$2>')
}

function referenceBlock(source: string | null, selection: string, comment: string, images?: string[]): string {
  const lines: string[] = ['<user-comment-reference>']
  if (source) lines.push(`<reference-source>${neutralizeTags(source.trim())}</reference-source>`)
  const cleanSelection = neutralizeTags(selection.trim())
  lines.push(`<quoted-text>${cleanSelection}</quoted-text>`)
  const cleanComment = neutralizeTags(comment.trim())
  if (cleanComment) {
    lines.push(`<user-comment>${cleanComment}</user-comment>`)
  }
  if (images?.length) {
    images.forEach((img, idx) => lines.push(`Attachment [Image ${idx + 1}]: ${img}`))
  }
  lines.push('</user-comment-reference>')
  return lines.join('\n')
}

/** Compact UI label for a file comment location (sidebar chips). */
export function formatCommentLocation(c: {
  lineStart?: number | null
  lineEnd?: number | null
  colIndex?: number | null
  colHeader?: string | null
  artifactSelector?: string | null
  artifactElementTag?: string | null
  selection?: string | null
}): string {
  if (c.artifactSelector) {
    return formatArtifactCommentLocation({
      elementTag: c.artifactElementTag,
      selector: c.artifactSelector,
      quote: c.selection,
    })
  }
  if (c.colIndex != null || (c.colHeader != null && c.colHeader !== '')) {
    const header = (c.colHeader || '').trim() || `Column ${(c.colIndex ?? 0) + 1}`
    if (c.lineStart) return `R${c.lineStart} · ${header}`
    return header
  }
  if (!c.lineStart) return ''
  if (!c.lineEnd || c.lineEnd === c.lineStart) return String(c.lineStart)
  return `${c.lineStart}-${c.lineEnd}`
}

function formatReferenceSource(c: FileCommentInput): string {
  let source = c.path
  if (c.artifactSelector) {
    const label = formatArtifactCommentLocation({
      elementTag: c.artifactElementTag,
      selector: c.artifactSelector,
      quote: c.selection,
    })
    if (label) source += ` (element ${label})`
    return source
  }
  if (c.colIndex != null || (c.colHeader != null && c.colHeader !== '')) {
    const header = (c.colHeader || '').trim() || `Column ${(c.colIndex ?? 0) + 1}`
    const letter = excelColLetter(c.colIndex ?? 0)
    if (c.lineStart) {
      source += ` (row ${c.lineStart}, column ${header} [${letter}])`
    } else {
      source += ` (column ${header} [${letter}])`
    }
    return source
  }
  if (c.lineStart) {
    source += c.lineEnd && c.lineEnd !== c.lineStart
      ? ` (lines ${c.lineStart}-${c.lineEnd})`
      : ` (line ${c.lineStart})`
  }
  return source
}

export function formatFileComments(comments: FileCommentInput[]): string {
  if (!comments.length) return ''
  return comments
    .map((c) => referenceBlock(formatReferenceSource(c), c.selection, c.comment, c.images))
    .join('\n')
}

function formatChatReferenceSource(c: ChatCommentInput): string | null {
  const parts: string[] = []
  if (c.messageRole) {
    parts.push(c.messageRole === 'user' ? 'user message' : 'assistant message')
  }
  if (c.paragraphIndex != null && c.paragraphIndex >= 0) {
    parts.push(`paragraph ${c.paragraphIndex + 1}`)
  }
  return parts.length ? parts.join(', ') : null
}

export function formatChatComments(comments: ChatCommentInput[]): string {
  if (!comments.length) return ''
  return comments
    .map((c) => referenceBlock(formatChatReferenceSource(c), c.selection, c.comment, c.images))
    .join('\n')
}
