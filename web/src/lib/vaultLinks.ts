// Resolution of in-vault markdown links for vault notes.
// Cross-note links are relative markdown links — `[Mo](./People/Mo.md)`, the
// destination relative to the containing note's directory. Resolution mirrors
// ciao/vault_index.py: relative to the current note first, then a vault-wide
// path/stem lookup with ambiguous stems left unresolved. The same machinery
// also resolves the bare refs in `related:`/`links:` frontmatter.

const MARKDOWN_EXT_RE = /\.(md|markdown)$/i
// A destination is external as soon as it carries a URI scheme. Bare and
// absolute paths are handled separately below.
const SCHEME_RE = /^[a-z][a-z0-9+.-]*:/i

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
  if (MARKDOWN_EXT_RE.test(s)) s = s.replace(MARKDOWN_EXT_RE, '')
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
    if (!MARKDOWN_EXT_RE.test(p)) continue
    const noExt = p.replace(MARKDOWN_EXT_RE, '')
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

export function resolveVaultLinkTarget(
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

/**
 * Reduce a markdown link destination to a vault note ref, or null when the
 * destination is not an in-vault note link and must render as a plain anchor.
 *
 * Rejected: scheme-qualified URLs (`https:`, `mailto:`, `javascript:`),
 * absolute and protocol-relative paths (`/x`, `//host` — the linter treats a
 * leading slash as non-local too), pure in-page anchors, and anything that
 * does not point at a markdown file.
 *
 * Any `#anchor`/`?query` suffix is dropped: nothing in the viewer scrolls to a
 * heading, and every resolved link renders as `href="#"` regardless.
 */
export function vaultNoteRefFromHref(href: string): string | null {
  const raw = (href || '').trim()
  if (!raw) return null
  if (raw.startsWith('#') || raw.startsWith('/')) return null
  if (SCHEME_RE.test(raw)) return null
  const path = raw.split('#')[0].split('?')[0]
  if (!path || !MARKDOWN_EXT_RE.test(path)) return null
  try {
    return decodeURIComponent(path)
  } catch {
    return path
  }
}
