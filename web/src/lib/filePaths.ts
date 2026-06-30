// Shared helpers for detecting file paths in assistant text and tool
// activity lines, and wrapping them in anchors that the delegated click
// handler in ChatPanel.vue reads to open the file viewer.
//
// We take a conservative approach: require at least one "/" and an allow-
// listed extension at the tail. This avoids common false positives like
// "v1.2/v3.4" or domains appearing in bare text.

const EXTS = [
  'md', 'markdown', 'txt',
  'py', 'ts', 'tsx', 'js', 'jsx', 'vue',
  'css', 'html', 'json',
  'yaml', 'yml', 'toml',
  'sh', 'rs', 'go', 'java', 'xml', 'sql',
  'cfg', 'ini', 'log', 'csv',
  'env', 'example', 'excalidraw',
]

// Path shape: optional leading "/", one or more `segment/`, then a final
// segment ending with an allow-listed extension, with an optional ":line".
// The non-capturing outer group rules out word-breaks inside extensions.
const EXT_ALT = EXTS.join('|')
export const FILE_PATH_RE = new RegExp(
  `(?<![\\w.])((?:\\/)?(?:[\\w.+-]+\\/)+[\\w.+-]+\\.(?:${EXT_ALT}))(?::(\\d+))?(?!\\w)`,
  'g',
)

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

function anchor(match: string, path: string, line: string | undefined): string {
  const dataLine = line ? ` data-line="${line}"` : ''
  return `<a class="file-link" href="#" data-file-path="${escapeHtml(path)}"${dataLine}>${escapeHtml(match)}</a>`
}

// True if the match at `matchIdx` is preceded (without intervening
// whitespace) by `://`, indicating it is the path portion of a URL.
function isInsideUrl(text: string, matchIdx: number): boolean {
  let i = matchIdx - 1
  let scanned = 0
  while (i >= 0 && scanned < 200) {
    const ch = text[i]
    if (ch === ' ' || ch === '\n' || ch === '\t' || ch === '\r' || ch === '<' || ch === '>') return false
    if (ch === ':' && text.slice(i, i + 3) === '://') return true
    i--
    scanned++
  }
  return false
}

/**
 * Linkify path-like substrings in plain text. Returns HTML.
 * Safe to feed into v-html: input is HTML-escaped before wrapping.
 */
export function linkifyText(text: string): string {
  if (!text) return ''
  let out = ''
  let last = 0
  const re = new RegExp(FILE_PATH_RE.source, 'g')
  let m: RegExpExecArray | null
  while ((m = re.exec(text)) !== null) {
    if (isInsideUrl(text, m.index)) continue
    out += escapeHtml(text.slice(last, m.index))
    out += anchor(m[0], m[1], m[2])
    last = m.index + m[0].length
  }
  out += escapeHtml(text.slice(last))
  return out
}

/**
 * Linkify path-like substrings in already-rendered HTML. Skips content
 * inside existing `<a>` tags (marked already wraps URLs) and inside
 * `<code>`/`<pre>` tags' attributes only — content inside <code> IS
 * processed because that's where Claude usually puts paths.
 *
 * This is intentionally a string walk, not a DOM parse: we operate on the
 * HTML string produced by marked, so we only need to avoid recursing into
 * existing <a> elements.
 */
export function linkifyHtml(html: string): string {
  if (!html) return ''
  let out = ''
  let i = 0
  const lower = html.toLowerCase()
  while (i < html.length) {
    // Skip existing <a ...>...</a> blocks as-is.
    if (lower.startsWith('<a', i)) {
      const close = lower.indexOf('</a>', i)
      if (close === -1) {
        out += html.slice(i)
        break
      }
      out += html.slice(i, close + 4)
      i = close + 4
      continue
    }
    // Advance to the next tag boundary; linkify the text span we skip over.
    const nextTag = html.indexOf('<', i)
    if (nextTag === -1) {
      out += linkifyTextWithinHtml(html.slice(i))
      break
    }
    if (nextTag > i) {
      out += linkifyTextWithinHtml(html.slice(i, nextTag))
    }
    // Copy the tag itself verbatim (both opening and closing tags).
    const tagEnd = html.indexOf('>', nextTag)
    if (tagEnd === -1) {
      out += html.slice(nextTag)
      break
    }
    out += html.slice(nextTag, tagEnd + 1)
    i = tagEnd + 1
  }
  return out
}

// Text-span linkifier used inside linkifyHtml. Unlike linkifyText it does
// NOT escape again, because the input already came from marked's escaped
// HTML output — re-escaping would turn "&amp;" into "&amp;amp;".
function linkifyTextWithinHtml(text: string): string {
  if (!text) return ''
  let out = ''
  let last = 0
  const re = new RegExp(FILE_PATH_RE.source, 'g')
  let m: RegExpExecArray | null
  while ((m = re.exec(text)) !== null) {
    if (isInsideUrl(text, m.index)) continue
    out += text.slice(last, m.index)
    out += anchor(m[0], m[1], m[2])
    last = m.index + m[0].length
  }
  out += text.slice(last)
  return out
}
