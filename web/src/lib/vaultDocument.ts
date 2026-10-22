import { parseFrontmatter } from './markdownFrontmatter'
import { renderFileMarkdown } from './safeMarkdown'
import { buildMarkdownIndex, joinRelative, resolveVaultLinkTarget } from './vaultLinks'

type Frontmatter = NonNullable<ReturnType<typeof parseFrontmatter>['frontmatter']>

export interface VaultMetadataItem {
  label: string
  path: string | null
}

export interface VaultMetadataList {
  key: string
  items: VaultMetadataItem[]
}

export interface VaultDocumentMetadata {
  body: string
  frontmatter: Frontmatter | null
  name: string
  showName: boolean
  type: string
  status: string
  tags: string[]
  created: string
  updated: string
  summary: string
  listExtras: VaultMetadataList[]
  extraEntries: { key: string; value: string }[]
}

const PRIMARY_KEYS = new Set(['title', 'name', 'type', 'status', 'tags', 'created', 'updated'])
const PROSE_KEYS = new Set(['description', 'summary', 'notes'])
const LIST_EXTRA_KEYS = new Set(['aliases', 'related', 'links'])
const LINK_LIST_KEYS = new Set(['related', 'links'])
const ABSOLUTE_SRC_RE = /^(?:[a-z][a-z0-9+.-]*:|\/\/|\/)/i

function frontmatterString(frontmatter: Frontmatter | null, key: string): string {
  const value = frontmatter?.[key]
  if (value == null) return ''
  return Array.isArray(value) ? value.join(', ') : String(value)
}

function frontmatterList(frontmatter: Frontmatter | null, key: string): string[] {
  const value = frontmatter?.[key]
  if (value == null) return []
  return Array.isArray(value) ? value : [String(value)]
}

function resolveListItem(
  raw: string,
  filePath: string,
  linkIndex: Map<string, string[]>,
  pathSet: Set<string>,
): VaultMetadataItem {
  const inner = raw.replace(/^\[\[(.+)\]\]$/, '$1').trim()
  const [ref, alias] = inner.split('|')
  const label = (alias ?? (ref.split('/').pop() || ref).replace(/\.md$/i, '')).trim()
  const path = ref.trim()
    ? resolveVaultLinkTarget(ref.trim(), filePath, linkIndex, pathSet)
    : null
  return { label, path }
}

/** Shared metadata view model for markdown notes in FileViewerModal and PinnedFilePanel. */
export function buildVaultDocumentMetadata(
  content: string,
  filePath: string,
  markdownPaths: string[],
): VaultDocumentMetadata {
  const split = parseFrontmatter(content)
  const frontmatter = split.frontmatter
  const getString = (key: string) => frontmatterString(frontmatter, key)
  const name = getString('title') || getString('name')
  const basename = filePath.split('/').pop() ?? filePath
  const heading = /^#\s+(.+?)\s*#*\s*$/m.exec(split.body || '')
  const showName = !!name.trim()
    && name.trim() !== basename.replace(/\.md$/, '')
    && (!heading || heading[1].trim() !== name.trim())

  let summary = ''
  for (const key of ['description', 'summary', 'notes']) {
    const value = getString(key)
    if (value.trim()) {
      summary = value
      break
    }
  }

  let linkIndex: Map<string, string[]> | undefined
  let pathSet: Set<string> | undefined
  const listExtras: VaultMetadataList[] = []
  for (const key of ['aliases', 'related', 'links']) {
    const items = frontmatterList(frontmatter, key)
    if (!items.length) continue
    let resolved: VaultMetadataItem[]
    if (LINK_LIST_KEYS.has(key)) {
      linkIndex ??= buildMarkdownIndex(markdownPaths)
      pathSet ??= new Set(markdownPaths)
      resolved = items.map(item => resolveListItem(item, filePath, linkIndex!, pathSet!))
    } else {
      resolved = [{ label: items.join(', '), path: null }]
    }
    listExtras.push({ key, items: resolved })
  }

  const extraEntries: { key: string; value: string }[] = []
  if (frontmatter) {
    const skip = new Set([...PRIMARY_KEYS, ...PROSE_KEYS, ...LIST_EXTRA_KEYS])
    for (const [key, value] of Object.entries(frontmatter)) {
      if (skip.has(key) || value == null) continue
      const text = Array.isArray(value) ? value.join(', ') : String(value)
      if (text.trim()) extraEntries.push({ key, value: text })
    }
  }

  return {
    body: split.body,
    frontmatter,
    name,
    showName,
    type: getString('type'),
    status: getString('status'),
    tags: frontmatterList(frontmatter, 'tags'),
    created: getString('created'),
    updated: getString('updated'),
    summary,
    listExtras,
    extraEntries,
  }
}

export function humanizeVaultMetadata(value: string): string {
  const text = String(value).replace(/[_-]+/g, ' ').trim()
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : text
}

export function isVaultMetadataUrl(value: string): boolean {
  return /^https?:\/\/\S+$/.test(value.trim())
}

/** Resolve a markdown image src relative to its vault note, preserving explicit URLs and root paths. */
export function resolveVaultImageSrc(filePath: string, href: string): string {
  if (!href || ABSOLUTE_SRC_RE.test(href)) return href
  const cleanPath = filePath.replace(/:\d+$/, '')
  const idx = cleanPath.lastIndexOf('/')
  const dir = idx === -1 ? '' : cleanPath.slice(0, idx + 1)
  const resolved = joinRelative(dir, href)
  return `/api/workspace-image?path=${encodeURIComponent(resolved)}`
}

/**
 * Render a note for the file viewer and the pinned-file panel.
 *
 * `openLocal` opts the surface into the openable dead link: a refused
 * `file:` link becomes a button the engine opens with the OS default app.
 * Only a surface whose click chain handles that button (the delegated
 * `handleOpenLocalFileClick`) may pass it.
 */
export function renderVaultMarkdown(
  body: string,
  filePath: string,
  markdownPaths: string[],
  openLocal = false,
): string {
  return renderFileMarkdown(body, {
    filePath,
    markdownPaths,
    resolveImageSrc: href => resolveVaultImageSrc(filePath, href),
    openLocal,
  })
}
