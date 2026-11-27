/**
 * The Unsure list as `GET /api/memory/unsure` answers it: the lines of
 * `<vault>/Workspace/Unsure.md`, parsed by the engine (`ciao/unsure_list.py`).
 * Pure helpers, no Vue: the store, the page and the rail share them.
 */

/** The kinds the stock memory guide allows; '' is a plain fact or change. */
export type UnsureKind = '' | 'observation' | 'drop' | 'restore' | 'category' | 'repo' | 'rule'

export interface UnsureItem {
  /** A hash of the exact line: the page names the line by it when it acts. */
  id: string
  checked: boolean
  kind: UnsureKind
  /** The region, repository path or rule key the kind names. */
  qualifier: string
  /** The item's first sentence. */
  title: string
  text: string
  reason: string
  source: string
  date: string
  /** The line exactly as the file has it. */
  raw: string
  /** Not a tick line (a plain bullet) or one with no text: shown raw, never applied. */
  malformed: boolean
}

export interface UnsureList {
  workspace: string
  /** Workspace-relative path of the file, for the viewer and a pinned chat. */
  path: string
  found: boolean
  items: UnsureItem[]
  counts: { waiting: number; ticked: number }
}

export type UnsureOp = 'tick' | 'untick' | 'remove'

/** The tag a row shows for its kind: short, plain, and never the only signal. */
export function kindLabel(kind: UnsureKind): string {
  switch (kind) {
    case '': return 'fact'
    case 'observation': return 'pattern'
    case 'drop': return 'trim'
    case 'restore': return 'restore'
    case 'category': return 'category'
    case 'repo': return 'repo'
    case 'rule': return 'rule'
  }
}

/**
 * Whether the next care run can apply a ticked line on its own. A new category,
 * a repository's AGENTS.md and a decision-rule flip are left for an attended
 * chat (curation-pass-1-unsure.md), so Approve opens that chat instead.
 */
export function appliesUnattended(item: Pick<UnsureItem, 'kind' | 'malformed'>): boolean {
  if (item.malformed) return false
  return item.kind !== 'category' && item.kind !== 'repo' && item.kind !== 'rule'
}

/** Why an item that cannot be applied unattended needs a chat, in plain words. */
export function needsChatReason(item: Pick<UnsureItem, 'kind' | 'malformed'>): string {
  if (item.malformed) return 'This line is not in the list\'s format, so care will not act on it.'
  switch (item.kind) {
    case 'category': return 'A new category is added in Settings, so this one is settled in a chat.'
    case 'repo': return 'This changes a code repository\'s instructions, so it is applied in a chat.'
    case 'rule': return 'This changes a decision rule, so it is applied in a chat.'
    default: return ''
  }
}

/** The opening draft of a chat about one item: its full line and where it came from. */
export function discussSeed(item: UnsureItem, listPath: string): string {
  const lines = [
    `Let's settle this item from my Unsure list (\`${listPath}\`):`,
    '',
    `> ${item.raw.replace(/^\s*[-*]\s*\[[ xX]\]\s*/, '')}`,
    '',
  ]
  if (item.source) lines.push(`It came from ${item.source}. Read that first.`)
  lines.push(
    'Tell me what you think is true and where it belongs. Once I decide, write it there '
    + '(or leave it out) and remove this line from the list.',
  )
  return lines.join('\n')
}
