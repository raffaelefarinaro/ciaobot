// Lightweight frontmatter splitter for the file viewers. Vault notes start
// with a `---` … `---` YAML block that the markdown viewers were rendering
// as a giant blob of paragraph text — unreadable and noisy. We split it off
// and let the viewer render a compact metadata card instead.
//
// We don't need full YAML parsing: vault frontmatter uses a tiny subset
// (scalar `key: value`, list items via `  - item`, simple strings with
// optional quotes). A hand-rolled parser keeps the bundle small and avoids
// surprises from a fuller library.

export type FrontmatterValue = string | string[] | null

export interface FrontmatterResult {
  frontmatter: Record<string, FrontmatterValue> | null
  body: string
}

/**
 * Split a markdown document into its frontmatter (if any) and the remaining
 * body. Frontmatter is detected only when the file starts with `---` on its
 * first line and a closing `---` exists on a later line.
 */
export function parseFrontmatter(source: string): FrontmatterResult {
  if (!source) return { frontmatter: null, body: source }

  // Allow a BOM or leading newlines but require `---` on the actual first
  // non-empty line. Without an opening fence we leave the document alone.
  const m = source.match(/^﻿?(?:\s*\r?\n)*---\r?\n/)
  if (!m) return { frontmatter: null, body: source }

  const after = source.slice(m[0].length)
  const closeMatch = after.match(/\r?\n---\s*(\r?\n|$)/)
  if (!closeMatch || closeMatch.index === undefined) {
    return { frontmatter: null, body: source }
  }

  const fmText = after.slice(0, closeMatch.index)
  const body = after.slice(closeMatch.index + closeMatch[0].length)

  const frontmatter = parseYamlSubset(fmText)
  return { frontmatter, body }
}

/**
 * Parse the limited YAML subset we use in vault frontmatter:
 *   key: value          → string
 *   key: "with quotes"  → string (quotes stripped)
 *   key:                → list, items follow on `  - item` lines
 *     - item1
 *     - item2
 *   key: [a, b]         → flow list, not used much but supported
 *
 * Anything we can't classify falls back to the raw string. Comments (`#`)
 * are stripped from the end of scalar lines.
 */
function parseYamlSubset(text: string): Record<string, FrontmatterValue> {
  const out: Record<string, FrontmatterValue> = {}
  const lines = text.split(/\r?\n/)
  let i = 0
  while (i < lines.length) {
    const raw = lines[i]
    const line = raw.replace(/\s+$/, '')
    if (!line.trim() || line.trim().startsWith('#')) { i++; continue }

    // Top-level `key: value` only — indented continuations are handled
    // inside the list-collection branch below.
    const match = line.match(/^([A-Za-z0-9_-]+)\s*:\s*(.*)$/)
    if (!match) { i++; continue }

    const key = match[1]
    const rest = stripInlineComment(match[2]).trim()

    if (!rest) {
      // Block list: collect subsequent indented `- item` lines.
      const items: string[] = []
      let j = i + 1
      while (j < lines.length) {
        const next = lines[j]
        if (!next.trim()) { j++; continue }
        const itemMatch = next.match(/^\s+-\s*(.*)$/)
        if (!itemMatch) break
        items.push(unquote(stripInlineComment(itemMatch[1]).trim()))
        j++
      }
      out[key] = items.length ? items : null
      i = j
      continue
    }

    // Flow list `[a, b, c]`
    if (rest.startsWith('[') && rest.endsWith(']')) {
      const inside = rest.slice(1, -1).trim()
      out[key] = inside ? inside.split(',').map(s => unquote(s.trim())) : []
      i++
      continue
    }

    out[key] = unquote(rest)
    i++
  }
  return out
}

function stripInlineComment(s: string): string {
  // Only strip ` # comment` (with leading whitespace) so URLs etc. stay intact.
  const idx = s.search(/\s#/)
  return idx === -1 ? s : s.slice(0, idx)
}

function unquote(s: string): string {
  if (s.length >= 2) {
    const first = s.charCodeAt(0)
    const last = s.charCodeAt(s.length - 1)
    if ((first === 34 || first === 39) && first === last) {
      return s.slice(1, -1)
    }
  }
  return s
}
