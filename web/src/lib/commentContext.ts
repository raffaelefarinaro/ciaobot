import { excelColLetter } from './csv'

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
  images?: string[]
}

export interface ChatCommentInput {
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
  if (source) lines.push(`<reference-source>${neutralizeTags(source)}</reference-source>`)
  lines.push('<quoted-text>', neutralizeTags(selection), '</quoted-text>')
  lines.push('<user-comment>', neutralizeTags(comment), '</user-comment>')
  if (images?.length) {
    // Images are also sent as real attachments; this manifest just preserves
    // the mapping of which image belongs to which comment.
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
}): string {
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

export function formatChatComments(comments: ChatCommentInput[]): string {
  if (!comments.length) return ''
  // No source anchor: the quoted text is from a reply already in the visible
  // conversation history the model can see, so a locator adds little.
  return comments.map((c) => referenceBlock(null, c.selection, c.comment, c.images)).join('\n')
}
