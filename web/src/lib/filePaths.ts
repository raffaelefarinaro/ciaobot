// Shared helpers for detecting file paths in assistant text and tool
// activity lines, and wrapping them in anchors that the delegated click
// handler in ChatPanel.vue reads to open the file viewer.
//
// We take a conservative approach: require at least one "/" and an allow-
// listed extension at the tail. This avoids common false positives like
// "v1.2/v3.4" or domains appearing in bare text.
//
// A second pass can linkify paths the agent actually touched (Write/Edit
// tool calls → `_filecard` rows) so basename-only mentions like
// "stradivari-ondevice-slide.pptx" still open the viewer even when the
// regex would miss them.

const EXTS = [
  'md', 'markdown', 'txt',
  'py', 'ts', 'tsx', 'js', 'jsx', 'vue',
  'css', 'html', 'json',
  'yaml', 'yml', 'toml',
  'sh', 'rs', 'go', 'java', 'xml', 'sql',
  'cfg', 'ini', 'log', 'csv',
  'env', 'example', 'excalidraw',
  // Binary / office types served by /api/workspace-binary (inline PDF preview).
  'pdf', 'pptx',
  // Images served by /api/workspace-image.
  'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'avif', 'bmp', 'ico',
]

// Path shape: optional leading "/", one or more `segment/`, then a final
// segment ending with an allow-listed extension, with an optional ":line".
// The non-capturing outer group rules out word-breaks inside extensions.
const EXT_ALT = EXTS.join('|')
export const FILE_PATH_RE = new RegExp(
  `(?<![\\w.])((?:\\/)?(?:[\\w.+-]+\\/)+[\\w.+-]+\\.(?:${EXT_ALT}))(?::(\\d+))?(?!\\w)`,
  'g',
)

type SpanMatch = { index: number; length: number; path: string; line?: string }

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

function anchorRaw(match: string, path: string, line: string | undefined): string {
  const dataLine = line ? ` data-line="${line}"` : ''
  return `<a class="file-link" href="#" data-file-path="${escapeHtml(path)}"${dataLine}>${match}</a>`
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

function overlaps(matches: SpanMatch[], index: number, length: number): boolean {
  const end = index + length
  return matches.some(m => index < m.index + m.length && end > m.index)
}

function basenameOf(filePath: string): string {
  const cleaned = filePath.replace(/[/\\]+$/, '')
  const slash = Math.max(cleaned.lastIndexOf('/'), cleaned.lastIndexOf('\\'))
  return slash >= 0 ? cleaned.slice(slash + 1) : cleaned
}

const EXTENSIONLESS_BASENAMES = new Set([
  'makefile',
  'dockerfile',
  'containerfile',
  'license',
  'licence',
  'readme',
  'changelog',
  'gemfile',
  'rakefile',
  'procfile',
  'vagrantfile',
  'gitignore',
  'dockerignore',
  'editorconfig',
  'npmrc',
  'browserslist',
])

/** Reject bare English words that were never a real path (e.g. "There"). */
export function isPlausibleFilePath(filePath: string): boolean {
  const trimmed = filePath.trim()
  if (!trimmed) return false
  if (/[*?$`\n]/.test(trimmed)) return false
  if (trimmed.includes('/') || trimmed.includes('\\') || trimmed.startsWith('.')) return true
  const base = basenameOf(trimmed)
  if (!base) return false
  if (/\.\w{1,20}$/.test(base)) return true
  return EXTENSIONLESS_BASENAMES.has(base.toLowerCase())
}

type KnownPathRule = { match: string; path: string; basenameOnly: boolean }

/** Build longest-first literal match rules from agent-touched file paths. */
export function buildKnownPathRules(knownPaths: string[]): KnownPathRule[] {
  const deduped = [...new Set(
    knownPaths.map(p => p.trim()).filter(p => p && isPlausibleFilePath(p)),
  )]
  if (!deduped.length) return []

  const basenameOwners = new Map<string, string>()
  for (const p of deduped) {
    const base = basenameOf(p)
    if (!base) continue
    const prev = basenameOwners.get(base)
    if (prev === undefined) basenameOwners.set(base, p)
    else if (prev !== p) basenameOwners.set(base, '')
  }

  const rules: KnownPathRule[] = []
  for (const p of deduped) {
    rules.push({ match: p, path: p, basenameOnly: false })
  }
  for (const [base, p] of basenameOwners) {
    if (!p) continue
    // Skip basename rule when it duplicates a full-path rule entry.
    if (deduped.includes(base)) continue
    // Never linkify bare words like "There" even if a bad _filecard exists.
    if (!isPlausibleFilePath(base)) continue
    rules.push({ match: base, path: p, basenameOnly: true })
  }
  rules.sort((a, b) => b.match.length - a.match.length)
  return rules
}

function hasBasenameBoundary(text: string, index: number, length: number): boolean {
  const before = index > 0 ? text[index - 1] : ''
  const after = index + length < text.length ? text[index + length] : ''
  if (before && /[\w./]/.test(before)) return false
  if (after && /\w/.test(after)) return false
  return true
}

function regexMatches(text: string): SpanMatch[] {
  const out: SpanMatch[] = []
  const re = new RegExp(FILE_PATH_RE.source, 'g')
  let m: RegExpExecArray | null
  while ((m = re.exec(text)) !== null) {
    if (isInsideUrl(text, m.index)) continue
    out.push({ index: m.index, length: m[0].length, path: m[1], line: m[2] })
  }
  return out
}

function knownPathMatches(text: string, rules: KnownPathRule[], taken: SpanMatch[]): SpanMatch[] {
  const out: SpanMatch[] = []
  const occupied = [...taken, ...out]
  for (const rule of rules) {
    let idx = 0
    while (idx < text.length) {
      const found = text.indexOf(rule.match, idx)
      if (found === -1) break
      if (isInsideUrl(text, found)
        || overlaps(occupied, found, rule.match.length)
        || (rule.basenameOnly && !hasBasenameBoundary(text, found, rule.match.length))) {
        idx = found + 1
        continue
      }
      const span: SpanMatch = { index: found, length: rule.match.length, path: rule.path }
      out.push(span)
      occupied.push(span)
      idx = found + rule.match.length
    }
  }
  return out
}

function mergeMatches(text: string, knownPaths: string[]): SpanMatch[] {
  const regex = regexMatches(text)
  const rules = buildKnownPathRules(knownPaths)
  const known = rules.length ? knownPathMatches(text, rules, regex) : []
  return [...regex, ...known].sort((a, b) => a.index - b.index || b.length - a.length)
}

function linkifySpan(text: string, knownPaths: string[] = [], escape: boolean): string {
  if (!text) return ''
  const matches = mergeMatches(text, knownPaths)
  if (!matches.length) return escape ? escapeHtml(text) : text

  let out = ''
  let last = 0
  for (const m of matches) {
    if (m.index < last) continue
    const slice = text.slice(last, m.index)
    out += escape ? escapeHtml(slice) : slice
    const display = text.slice(m.index, m.index + m.length)
    out += escape ? anchor(display, m.path, m.line) : anchorRaw(display, m.path, m.line)
    last = m.index + m.length
  }
  const tail = text.slice(last)
  out += escape ? escapeHtml(tail) : tail
  return out
}

/**
 * Linkify path-like substrings in plain text. Returns HTML.
 * Safe to feed into v-html: input is HTML-escaped before wrapping.
 */
export function linkifyText(text: string, knownPaths: string[] = []): string {
  return linkifySpan(text, knownPaths, true)
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
export function linkifyHtml(html: string, knownPaths: string[] = []): string {
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
      out += linkifySpan(html.slice(i), knownPaths, false)
      break
    }
    if (nextTag > i) {
      out += linkifySpan(html.slice(i, nextTag), knownPaths, false)
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
