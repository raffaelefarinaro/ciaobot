import { COMMENT_TAGS } from './commentContext'

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
  'env', 'example',
  // Binary / office types served by /api/workspace-binary (inline PDF preview).
  'pdf', 'pptx', 'mht', 'mhtml',
  // Images served by /api/workspace-image.
  'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'avif', 'bmp', 'ico',
]

// Path shape: optional leading "/", one or more `segment/`, then a final
// segment ending with an allow-listed extension, with an optional ":line".
// The non-capturing outer group rules out word-breaks inside extensions.
const EXT_ALT = EXTS.join('|')
const FILE_PATH_RE = new RegExp(
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

const KNOWN_EXTS = new Set(EXTS)

/** True when the basename ends in an extension the file viewer can open.
 *
 * Used to break the tie a bare `name.ext` cannot resolve on shape alone:
 * `notes.md` is a file and `anthropic.com` is a site, and both are a single
 * segment with a dotted suffix. (`.md` is also Moldova's TLD; the allowlist
 * decides which reading wins, and files win.)
 */
export function hasKnownFileExtension(filePath: string): boolean {
  const base = basenameOf(filePath.trim())
  const dot = base.lastIndexOf('.')
  if (dot <= 0) return false
  return KNOWN_EXTS.has(base.slice(dot + 1).toLowerCase())
}

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

// A first segment shaped like a host (`example.com/pricing`) is a web link
// written without its scheme, not a folder. Accepted misses: `localhost/foo`
// and a host whose "TLD" is a file extension (`example.sh/x`) read as folders.
const DOMAIN_SEGMENT_RE = /^(?:[a-z0-9-]+\.)+[a-z]{2,}$/i
const DOTTED_EXT_RE = /\.[^./\\]+$/

/**
 * True when a relative markdown link target names a folder: `exports/sloth-v5`, `exports/`.
 * Not claimed: a dotted last segment (`exports/v1.2`, a file or a dotted folder),
 * a known extensionless file name (`docs/Makefile`), and dot-only paths (`./`, `../`).
 */
export function isFolderLinkPath(href: string): boolean {
  const raw = href.trim()
  if (!raw || raw.startsWith('/') || raw.startsWith('#') || raw.startsWith('~')) return false
  if (/^[a-z][a-z0-9+.-]*:/i.test(raw)) return false
  if (/[*?$`<>"|\n]/.test(raw)) return false
  const trailing = /[/\\]$/.test(raw)
  const segments = raw.replace(/[/\\]+$/, '').split(/[/\\]/)
  if (segments.some(s => !s)) return false
  if (segments.every(s => s === '.' || s === '..')) return false
  if (segments.length < 2 && !trailing) return false
  if (DOMAIN_SEGMENT_RE.test(segments[0]) && !hasKnownFileExtension(segments[0])) return false
  if (trailing) return true
  const last = segments[segments.length - 1]
  return !DOTTED_EXT_RE.test(last) && !EXTENSIONLESS_BASENAMES.has(last.toLowerCase())
}

/** True when a clicked link might be a folder and is worth asking the engine about. */
export function looksLikeFolderPath(filePath: string): boolean {
  const clean = filePath.trim().replace(/:\d+$/, '')
  if (!clean) return false
  const segments = clean.replace(/[/\\]+$/, '').split(/[/\\]/)
  if (segments.every(s => s === '' || s === '.' || s === '..')) return false
  if (/[/\\]$/.test(clean)) return true
  const base = basenameOf(clean)
  return !DOTTED_EXT_RE.test(base) && !EXTENSIONLESS_BASENAMES.has(base.toLowerCase())
}

type KnownPathRule = { match: string; path: string; basenameOnly: boolean }

/** Build longest-first literal match rules from agent-touched file paths. */
function computeKnownPathRules(knownPaths: string[]): KnownPathRule[] {
  const dedupedSet = new Set(
    knownPaths.map(p => p.trim()).filter(p => p && isPlausibleFilePath(p)),
  )
  if (!dedupedSet.size) return []
  const deduped = [...dedupedSet]

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
    // Skip basename rule when it duplicates a full-path rule entry. Set
    // membership, not Array.includes: this loop runs once per distinct
    // basename, and a linear scan per iteration made rule-building
    // quadratic in the number of touched files (issue #501).
    if (dedupedSet.has(base)) continue
    // Never linkify bare words like "There" even if a bad _filecard exists.
    if (!isPlausibleFilePath(base)) continue
    rules.push({ match: base, path: p, basenameOnly: true })
  }
  rules.sort((a, b) => b.match.length - a.match.length)
  return rules
}

// Rules are derived purely from the path list, and the whole transcript
// renders against one list, so building them once per list instead of once
// per text span is the difference between linear and quadratic growth in a
// long chat: the list grows with the transcript and every message paid for a
// full rebuild. Keyed on array identity — the caller (`ChatPanel`'s
// `knownFilePaths`) is a computed that yields a fresh array whenever the set
// changes, so a mutated-in-place array is the one shape this would not
// notice; callers must hand over a new array rather than push into an old one.
const rulesCache = new WeakMap<readonly string[], KnownPathRule[]>()

// How many times the rules were actually computed rather than served from
// the cache. Exported so `renderScaling.test.ts` can pin the work a render
// does instead of timing it: a duration is machine- and load-dependent, this
// number is not, and "one build per path list" is the whole invariant.
let rulesBuildCount = 0

export function knownPathRulesBuildCount(): number {
  return rulesBuildCount
}

export function buildKnownPathRules(knownPaths: string[]): KnownPathRule[] {
  const cached = rulesCache.get(knownPaths)
  if (cached) return cached
  rulesBuildCount++
  const rules = computeKnownPathRules(knownPaths)
  rulesCache.set(knownPaths, rules)
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

const MAX_LINE = 2147483647
const LINE_NUMBER_RE = /^[1-9]\d{0,9}$/
// Sticky, so the lookahead reads only the characters right after a match.
const LINE_SUFFIX_AT_RE = /:([1-9]\d{0,9})(?!\w)/y

function isLineNumber(digits: string): boolean {
  return LINE_NUMBER_RE.test(digits) && Number(digits) <= MAX_LINE
}

/** The valid `:line` digits that start at `at`, if any. */
function lineSuffixAt(text: string, at: number): string | undefined {
  LINE_SUFFIX_AT_RE.lastIndex = at
  const m = LINE_SUFFIX_AT_RE.exec(text)
  return m && isLineNumber(m[1]) ? m[1] : undefined
}

function knownPathMatches(text: string, rules: KnownPathRule[], taken: SpanMatch[]): SpanMatch[] {
  const out: SpanMatch[] = []
  const occupied = [...taken, ...out]
  for (const rule of rules) {
    let idx = 0
    while (idx < text.length) {
      const found = text.indexOf(rule.match, idx)
      if (found === -1) break
      // A touched path followed by `:17` keeps its navigation line in the link.
      const line = lineSuffixAt(text, found + rule.match.length)
      const length = rule.match.length + (line ? line.length + 1 : 0)
      if (isInsideUrl(text, found)
        || overlaps(occupied, found, length)
        || (rule.basenameOnly && !hasBasenameBoundary(text, found, rule.match.length))) {
        idx = found + 1
        continue
      }
      const span: SpanMatch = { index: found, length, path: rule.path, line }
      out.push(span)
      occupied.push(span)
      idx = found + rule.match.length
    }
  }
  return out
}

function mergeMatches(text: string, rules: KnownPathRule[]): SpanMatch[] {
  const regex = regexMatches(text)
  const known = rules.length ? knownPathMatches(text, rules, regex) : []
  return [...regex, ...known].sort((a, b) => a.index - b.index || b.length - a.length)
}

function linkifySpan(text: string, rules: KnownPathRule[], escape: boolean): string {
  if (!text) return ''
  const matches = mergeMatches(text, rules)
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
  return linkifySpan(text, buildKnownPathRules(knownPaths), true)
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
 *
 * With `fileMentionTargets` the document is walked as DOM instead, and only
 * known touched paths and exactly confirmed mentions link (see
 * `linkifyChecked`).
 */
export function linkifyHtml(html: string, knownPaths: string[] = [], fileMentionTargets?: FileMentionTargets): string {
  if (!html) return ''
  // One rule set for every span in this document, not one per span.
  const rules = buildKnownPathRules(knownPaths)
  if (fileMentionTargets) return linkifyChecked(html, rules, fileMentionTargets)
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
    // Skip <button ...>...</button> blocks as well. The openable dead-link
    // button (lib/safeMarkdown.ts) carries a path as visible text, and
    // wrapping that text in an inner `<a class="file-link">` would intercept
    // the click. Text inside a button is otherwise plain button markup, not
    // prose.
    if (lower.startsWith('<button', i)) {
      const close = lower.indexOf('</button>', i)
      if (close === -1) {
        out += html.slice(i)
        break
      }
      out += html.slice(i, close + 8)
      i = close + 8
      continue
    }
    // Advance to the next tag boundary; linkify the text span we skip over.
    const nextTag = html.indexOf('<', i)
    if (nextTag === -1) {
      out += linkifySpan(html.slice(i), rules, false)
      break
    }
    if (nextTag > i) {
      out += linkifySpan(html.slice(i, nextTag), rules, false)
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

// --- Checked file mentions -------------------------------------------------
//
// A completed assistant message may link an untouched filename, but only one
// a browser lookup already confirmed: the caller passes the confirmed map and
// a candidate links only when its exact display spelling is a key. Nothing
// here reads the network or a store.

/** Confirmed mention spelling (the display text, with any `:line`) to canonical path. */
export type FileMentionTargets = ReadonlyMap<string, string>

/** One mention found in text. `input` is the exact display spelling. */
export type FileMentionSpan = {
  index: number
  length: number
  input: string
  line?: string
}

// Discovery list, deliberately separate from EXTS: EXTS also feeds the legacy
// regex and folder detection. `env` and `example` are left out on purpose.
const DISCOVERY_EXTS = new Set([
  'md', 'markdown', 'txt',
  'py', 'ts', 'tsx', 'js', 'jsx', 'vue',
  'css', 'html', 'htm', 'json',
  'yaml', 'yml', 'toml',
  'sh', 'rs', 'go', 'java', 'xml', 'sql',
  'cfg', 'ini', 'log', 'csv',
  'pdf', 'pptx', 'mht', 'mhtml',
  'png', 'jpg', 'jpeg', 'gif', 'webp', 'svg', 'avif', 'bmp', 'ico',
])

// Characters that make a token a glob, an env expansion, a URL-ish or
// otherwise not a plain path. Quotes and ampersands are allowed: they are
// filename characters inside a code span, and the renderer escapes them.
const NOT_PATH_CHARS = /[\x00-\x1f\x7f*?$[\]{}<>|`@]/
// Prose delimiters around a token: they are trimmed, never part of a path.
const LEAD_DELIM = /^[([{<"'`*]+/
const TRAIL_DELIM = /[)\]}>"'`*.,;:!?]+$/

function isPathCandidate(p: string, wholeCode: boolean): boolean {
  if (!p || NOT_PATH_CHARS.test(p) || p.includes('://') || p.includes('//') || p.includes('\\\\')) return false
  // The only anchors a whole path may start with: a drive, `~/`, `/`, `./` or `../`.
  const prefix = /^(?:[A-Za-z]:[\\/]|~[\\/]|[\\/]|\.{1,2}[\\/])/.exec(p)
  const rest = prefix ? p.slice(prefix[0].length) : p
  // Any other colon (`C:src`, `host:path`) is not a path shape.
  if (rest.includes(':')) return false
  // `~otheruser/...` is somebody else's home.
  if (!prefix && p.startsWith('~')) return false
  const segments = rest.split(/[\\/]/)
  if (segments.some(s => !s)) return false
  const name = segments[segments.length - 1]
  const dot = name.lastIndexOf('.')
  if (dot <= 0 || !DISCOVERY_EXTS.has(name.slice(dot + 1).toLowerCase())) return false
  // In a code span a space is allowed inside a path, but a relative path whose
  // first word has a space reads as a command followed by a path: `cat a.md`.
  if (wholeCode && !prefix && /\s/.test(p.split(/[\\/]/)[0])) return false
  return true
}

/** A candidate with its optional `:line` split off, or null when it is not a path. */
function mentionAt(input: string, index: number, wholeCode: boolean): FileMentionSpan | null {
  const suffix = /:(\d+)$/.exec(input)
  if (suffix && !isLineNumber(suffix[1])) return null
  const path = suffix ? input.slice(0, suffix.index) : input
  if (!isPathCandidate(path, wholeCode)) return null
  return { index, length: input.length, input, line: suffix ? suffix[1] : undefined }
}

/**
 * The file mentions in `text`, in order. Prose is split on whitespace and a
 * token counts only when it is one whole path. A whole inline code span
 * (`wholeCodeSpan`) is one path, possibly containing spaces.
 */
export function findFileMentionSpans(text: string, wholeCodeSpan = false): FileMentionSpan[] {
  if (wholeCodeSpan) {
    const span = mentionAt(text.trim(), text.length - text.trimStart().length, true)
    return span ? [span] : []
  }
  const out: FileMentionSpan[] = []
  for (const m of text.matchAll(/\S+/g)) {
    const raw = m[0]
    const lead = LEAD_DELIM.exec(raw)?.[0].length ?? 0
    const core = raw.slice(lead).replace(TRAIL_DELIM, '')
    if (!core) continue
    const span = mentionAt(core, m.index + lead, false)
    if (span) out.push(span)
  }
  return out
}

/** An element the text run runs straight into. A line break starts a new token. */
function isGluingElement(node: Node | null): boolean {
  return !!node && node.nodeType === Node.ELEMENT_NODE && (node as Element).localName !== 'br'
}

/**
 * The mentions in one DOM text run. A token that continues into a neighbouring
 * element is not whole: `a@b.co/x.md` after an autolinked address leaves
 * `/x.md` glued to the link, and that suffix is not a path of its own.
 */
export function runMentionSpans(text: string, wholeCode: boolean, prev: Node | null, next: Node | null): FileMentionSpan[] {
  const gluedStart = isGluingElement(prev)
  const gluedEnd = isGluingElement(next)
  return findFileMentionSpans(text, wholeCode).filter(s =>
    !(gluedStart && s.index === 0) && !(gluedEnd && s.index + s.length === text.length),
  )
}

function anchorNode(display: string, path: string, line: string | undefined): HTMLAnchorElement {
  const el = document.createElement('a')
  el.className = 'file-link'
  el.setAttribute('href', '#')
  el.setAttribute('data-file-path', path)
  if (line) el.setAttribute('data-line', line)
  el.textContent = display
  return el
}

/**
 * The nodes that replace one text run: known touched paths first, then the
 * confirmed mentions that do not overlap them. null when nothing links.
 */
function mentionNodes(
  text: string,
  rules: KnownPathRule[],
  targets: FileMentionTargets | null,
  wholeCode: boolean,
  prev: Node | null = null,
  next: Node | null = null,
): Node[] | null {
  if (!text) return null
  const known = rules.length ? knownPathMatches(text, rules, []) : []
  const found: SpanMatch[] = []
  if (targets?.size) {
    for (const s of runMentionSpans(text, wholeCode, prev, next)) {
      const path = targets.get(s.input)
      if (path === undefined || overlaps(known, s.index, s.length)) continue
      found.push({ index: s.index, length: s.length, path, line: s.line })
    }
  }
  const matches = [...known, ...found].sort((a, b) => a.index - b.index)
  if (!matches.length) return null
  const nodes: Node[] = []
  let last = 0
  for (const m of matches) {
    if (m.index > last) nodes.push(document.createTextNode(text.slice(last, m.index)))
    nodes.push(anchorNode(text.slice(m.index, m.index + m.length), m.path, m.line))
    last = m.index + m.length
  }
  if (last < text.length) nodes.push(document.createTextNode(text.slice(last)))
  return nodes
}

const CHECKED_SKIP_TAGS = new Set(['a', 'button', 'script', 'style', 'img'])
const COMMENT_TAG_SET = new Set<string>(COMMENT_TAGS)

/**
 * Checked walk over parsed HTML. Text decodes into real text nodes before
 * matching; new nodes come from DOM calls, never from interpolated markup.
 * Existing anchors, buttons, scripts, styles and images are skipped whole.
 * Fenced code and quoted-comment subtrees get known rules only: discovery
 * never reads them.
 */
function linkifyChecked(html: string, rules: KnownPathRule[], targets: FileMentionTargets): string {
  const template = document.createElement('template')
  template.innerHTML = html
  linkifyChildren(template.content, rules, targets, false)
  return template.innerHTML
}

function linkifyChildren(parent: Node, rules: KnownPathRule[], targets: FileMentionTargets, knownOnly: boolean): void {
  for (const child of Array.from(parent.childNodes)) {
    if (child.nodeType === Node.TEXT_NODE) {
      const nodes = mentionNodes(child.textContent ?? '', rules, knownOnly ? null : targets, false, child.previousSibling, child.nextSibling)
      if (nodes) (child as ChildNode).replaceWith(...nodes)
      continue
    }
    if (child.nodeType !== Node.ELEMENT_NODE) continue
    const el = child as Element
    const tag = el.localName
    if (CHECKED_SKIP_TAGS.has(tag)) continue
    const inner = knownOnly || tag === 'pre' || COMMENT_TAG_SET.has(tag)
    // An inline code span holding only text is one candidate: its whole text.
    if (!inner && tag === 'code' && Array.from(el.childNodes).every(n => n.nodeType === Node.TEXT_NODE)) {
      const nodes = mentionNodes(el.textContent ?? '', rules, targets, true)
      if (nodes) el.replaceChildren(...nodes)
      continue
    }
    linkifyChildren(el, rules, targets, inner)
  }
}
