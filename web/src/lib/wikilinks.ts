// Obsidian-style [[wikilink]] parsing and resolution for vault markdown.
// Resolution mirrors ciao/vault_index.py: relative to the current note first,
// then vault-wide path/stem lookup with ambiguous stems left unresolved.

export type WikilinkMatch = {
  ref: string
  display: string
  anchor: string | null
}

const WIKILINK_RE = /\[\[([^\]|#]+)(?:#([^\]|]*))?(?:\|([^\]]*))?\]\]/g
const FENCED_CODE_RE = /```[\s\S]*?```/g
const INLINE_CODE_RE = /`[^`\n]*`/g

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

function escapeAttr(s: string): string {
  return escapeHtml(s)
}

export function joinRelative(dir: string, rel: string): string {
  const parts = (dir + rel).split('/')
  const out: string[] = []
  for (const p of parts) {
    if (p === '' || p === '.') continue
    if (p === '..') { out.pop(); continue }
    out.push(p)
  }
  return out.join('/')
}

export function docDirFor(filePath: string): string {
  const cleaned = filePath.replace(/:\d+$/, '')
  const idx = cleaned.lastIndexOf('/')
  return idx === -1 ? '' : cleaned.slice(0, idx + 1)
}

function normalizeRef(ref: string): string {
  let s = ref.trim()
  if (s.startsWith('memory-vault/')) s = s.slice('memory-vault/'.length)
  if (/\.(md|markdown)$/i.test(s)) s = s.replace(/\.(md|markdown)$/i, '')
  return s
}

function addIndexEntry(index: Map<string, string[]>, key: string, path: string): void {
  const hits = index.get(key)
  if (hits) {
    if (!hits.includes(path)) hits.push(path)
  } else {
    index.set(key, [path])
  }
}

/** Build lookup keys from workspace-relative markdown paths. */
export function buildMarkdownIndex(paths: string[]): Map<string, string[]> {
  const index = new Map<string, string[]>()
  for (const p of paths) {
    if (!/\.(md|markdown)$/i.test(p)) continue
    const noExt = p.replace(/\.(md|markdown)$/i, '')
    addIndexEntry(index, noExt, p)
    const stem = noExt.split('/').pop()
    if (stem) addIndexEntry(index, stem, p)
    const vaultIdx = noExt.indexOf('memory-vault/')
    if (vaultIdx >= 0) {
      addIndexEntry(index, noExt.slice(vaultIdx + 'memory-vault/'.length), p)
    }
  }
  return index
}

export function resolveWikilinkTarget(
  ref: string,
  filePath: string,
  index: Map<string, string[]>,
  pathSet: Set<string>,
): string | null {
  const normalized = normalizeRef(ref)
  if (!normalized) return null

  const dir = docDirFor(filePath)
  const relativeCandidates = [
    joinRelative(dir, `${normalized}.md`),
    joinRelative(dir, `${normalized}.markdown`),
  ]
  for (const candidate of relativeCandidates) {
    if (pathSet.has(candidate)) return candidate
  }

  const direct = index.get(normalized)
  if (direct?.length === 1) return direct[0]
  if (direct && direct.length > 1) {
    const relativePick = direct.find(p => relativeCandidates.includes(p))
    if (relativePick) return relativePick
    if (normalized.includes('/')) return direct[0]
    return null
  }

  const tail = normalized.split('/').pop() || normalized
  const stemHits = index.get(tail)
  if (stemHits?.length === 1) return stemHits[0]

  return null
}

export function extractWikilinks(text: string): WikilinkMatch[] {
  const out: WikilinkMatch[] = []
  let m: RegExpExecArray | null
  const re = new RegExp(WIKILINK_RE.source, 'g')
  while ((m = re.exec(text)) !== null) {
    const ref = m[1]?.trim() || ''
    if (!ref) continue
    const anchor = m[2]?.trim() || null
    const display = (m[3]?.trim() || ref.split('/').pop() || ref).trim()
    out.push({ ref, display, anchor })
  }
  return out
}

function linkifyPlainSegment(
  text: string,
  filePath: string,
  index: Map<string, string[]>,
  pathSet: Set<string>,
): string {
  return text.replace(WIKILINK_RE, (_match, rawRef: string, _anchor: string | undefined, rawDisplay: string | undefined) => {
    const ref = rawRef.trim()
    if (!ref) return _match
    const label = (rawDisplay?.trim() || ref.split('/').pop() || ref).trim()
    const target = resolveWikilinkTarget(ref, filePath, index, pathSet)
    if (!target) {
      return `<span class="wikilink-unresolved" title="${escapeAttr(ref)}">${escapeHtml(label)}</span>`
    }
    return `<a class="file-link wikilink" href="#" data-file-path="${escapeAttr(target)}">${escapeHtml(label)}</a>`
  })
}

function splitAndTransform(text: string, skipRes: RegExp[], transform: (segment: string) => string): string {
  const tokens: { kind: 'code' | 'text'; value: string }[] = []
  let i = 0
  while (i < text.length) {
    let earliest: { index: number; length: number; re: RegExp } | null = null
    for (const re of skipRes) {
      re.lastIndex = i
      const m = re.exec(text)
      if (!m || m.index < i) continue
      if (!earliest || m.index < earliest.index) {
        earliest = { index: m.index, length: m[0].length, re }
      }
    }
    if (!earliest) {
      tokens.push({ kind: 'text', value: text.slice(i) })
      break
    }
    if (earliest.index > i) {
      tokens.push({ kind: 'text', value: text.slice(i, earliest.index) })
    }
    tokens.push({ kind: 'code', value: text.slice(earliest.index, earliest.index + earliest.length) })
    i = earliest.index + earliest.length
  }
  return tokens.map(t => (t.kind === 'code' ? t.value : transform(t.value))).join('')
}

/** Replace body wikilinks with file-link anchors before markdown parsing. */
export function linkifyWikilinksInMarkdown(
  text: string,
  filePath: string,
  markdownPaths: string[],
): string {
  if (!text || !filePath || !markdownPaths.length) return text
  const index = buildMarkdownIndex(markdownPaths)
  const pathSet = new Set(markdownPaths.filter(p => /\.(md|markdown)$/i.test(p)))
  return splitAndTransform(
    text,
    [FENCED_CODE_RE, INLINE_CODE_RE],
    segment => linkifyPlainSegment(segment, filePath, index, pathSet),
  )
}
