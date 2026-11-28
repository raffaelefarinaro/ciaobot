/**
 * The Unsure list as `GET /api/memory/unsure` answers it: the lines of
 * `<vault>/Workspace/Unsure.md`, parsed by the engine (`ciao/unsure_list.py`).
 * Pure helpers, no Vue: the store, the page and the rail share them.
 */
import { isChatId } from './chatLinks'
import { resolveUnder, sourceKey, type ReviewSource } from './reviewQueue'

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
  /** Where an approved line goes (its `to:` field): a vault note path or a region; '' when the line does not say. */
  destination: string
  /** The engine's check of `destination`: a region it may name or a vault note outside `Workspace/`, `automations/` and the guide. False when empty. */
  destination_ok: boolean
  /** The heading the line sits under ('' for none), so an Undo puts it back there. */
  section: string
  /** The quoted line after the first sentence: what approving writes, or the entry it removes or puts back; '' when none. */
  proposal: string
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


// ── The review queue's reading of one item ───────────────────────────────────

const CHAT_IDS = /chat-[0-9a-f]{8}\b/g


/**
 * Where the item came from, in order and without repeats: the `source:` field
 * (chat ids and note paths joined by `,` or `and`) and, on an observation, the
 * chats its `seen in:` detail names.
 */
export function unsureSources(item: Pick<UnsureItem, 'source' | 'text' | 'kind'>): ReviewSource[] {
  const out: ReviewSource[] = []
  const seen = new Set<string>()
  const add = (source: ReviewSource) => {
    const key = sourceKey(source)
    if (!key || seen.has(key)) return
    seen.add(key)
    out.push(source)
  }
  for (const piece of item.source.split(/\s*,\s*|\s+and\s+/)) {
    const token = piece.trim()
    if (!token) continue
    if (isChatId(token)) add({ kind: 'chat', id: token })
    else if (/\.md$/i.test(token) && !token.startsWith('/') && !token.includes('..')) add({ kind: 'note', path: token })
    else add({ kind: 'text', label: token })
  }
  if (item.kind === 'observation') {
    const seenIn = /—\s*seen in:\s*([^—]*)/.exec(item.text)?.[1] ?? ''
    for (const id of seenIn.match(CHAT_IDS) ?? []) add({ kind: 'chat', id })
  }
  return out
}

/** How many distinct chats the item names. */
export function unsureChatCount(item: Pick<UnsureItem, 'source' | 'text' | 'kind'>): number {
  return unsureSources(item).filter(s => s.kind === 'chat').length
}

/**
 * The item's words after its first sentence, without what the detail pane
 * shows elsewhere: the quoted line (*If you approve*), an observation's
 * `seen in:` chats (*Where it came from*) and a rule's `old -> new`.
 */
export function unsureMore(item: Pick<UnsureItem, 'title' | 'text' | 'proposal' | 'kind'>): string {
  let rest = item.text.startsWith(item.title) ? item.text.slice(item.title.length) : ''
  rest = rest.replace(/\s*—\s*seen in:[^—]*/i, '')
  if (item.proposal) rest = rest.replace(`"${item.proposal}"`, '').replace(`“${item.proposal}”`, '')
  if (item.kind === 'rule') rest = rest.replace(/\S+\s*->\s*\S+\s*$/, '')
  return rest.replace(/\s+/g, ' ').trim()
}

/** A rule flip's `old -> new`, when the line carries one. */
export function ruleChange(item: Pick<UnsureItem, 'text' | 'kind'>): { from: string; to: string } | null {
  if (item.kind !== 'rule') return null
  const match = /(\S+)\s*->\s*(\S+)\s*$/.exec(item.text)
  return match ? { from: match[1], to: match[2] } : null
}

/** One run of an item's reason: plain words, or a Markdown link's text with the vault file it points at. */
export interface ReasonPart { text: string; path?: string }

/** The vault folder of the list (`<vault>/Workspace/Unsure.md` → `<vault>`), as the file viewer opens paths. */
export function unsureVaultRoot(listPath: string): string {
  return listPath.replace(/\/?Workspace\/Unsure\.md$/, '')
}


/**
 * The reason, with each Markdown link turned into its text and, when it points
 * at a Markdown file inside the vault, that file (resolved from the list's own
 * folder, as the link was written).
 */
export function reasonParts(reason: string, listPath: string): ReasonPart[] {
  const root = unsureVaultRoot(listPath)
  const folder = listPath.replace(/\/[^/]*$/, '')
  const parts: ReasonPart[] = []
  const link = /\[([^\]]+)\]\(([^)\s]*)\)/g
  let at = 0
  for (let match = link.exec(reason); match; match = link.exec(reason)) {
    if (match.index > at) parts.push({ text: reason.slice(at, match.index) })
    const path = /\.md(#.*)?$/i.test(match[2]) ? resolveUnder(root, folder, match[2]) : ''
    parts.push(path ? { text: match[1], path } : { text: match[1] })
    at = match.index + match[0].length
  }
  if (at < reason.length) parts.push({ text: reason.slice(at) })
  return parts
}

/** A vault-relative note path (a source or a destination) as the file viewer opens it; '' when unsafe. */
export function vaultFile(listPath: string, path: string): string {
  const root = unsureVaultRoot(listPath)
  return resolveUnder(root, root, path)
}

/** `2026-11-27` as `Nov 27` (a different year adds it); '' for anything else. */
export function foundDate(date: string, now = new Date()): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date)
  if (!match) return ''
  const day = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])))
  if (Number.isNaN(day.getTime())) return ''
  const sameYear = day.getUTCFullYear() === now.getFullYear()
  return day.toLocaleDateString('en-US', { month: 'short', day: 'numeric', ...(sameYear ? {} : { year: 'numeric' }), timeZone: 'UTC' })
}

/**
 * What approving this item does, in the page's words, from what the line
 * itself says and the stock guide's table of kinds (unsure-and-proposals.md):
 * `line` is the quoted line when the line carries one (never invented),
 * `lineRole` whether approving writes, removes or puts it back, `where` the
 * destination (the `to:` field, or the region a drop or restore names), and
 * `how` one sentence on who does it and when.
 */
export interface ApproveEffect {
  chat: boolean
  /** The line names a destination the engine refuses: it is shown as not allowed and cannot be approved here. */
  blocked: boolean
  line: string
  lineRole: 'write' | 'remove' | 'restore'
  where: string
  /** `where` is a vault note the viewer can open. */
  whereIsNote: boolean
  how: string
}

export function approveEffect(item: UnsureItem): ApproveEffect {
  const destination = item.destination
  const isNote = /\.md$/i.test(destination) && item.destination_ok
  const base = { line: item.proposal, lineRole: 'write' as const, where: destination, whereIsNote: isNote, blocked: false }
  if (destination && !item.destination_ok && (item.kind === '' || item.kind === 'observation') && !item.malformed) {
    return {
      ...base, chat: false, blocked: true,
      how: 'This line names a place Ciaobot will not write to, so it cannot be approved here. Dismiss it, or discuss it.',
    }
  }
  if (item.malformed) {
    return { ...base, chat: true, line: '', where: '', whereIsNote: false, how: 'This line is not in the list\'s format, so care will not act on it. Approve opens a chat about it.' }
  }
  switch (item.kind) {
    case 'drop':
      return {
        ...base, chat: false, lineRole: 'remove', where: item.qualifier, whereIsNote: false,
        how: `The next Workspace care run removes it from ${item.qualifier ? `the ${item.qualifier} region` : 'memory'}, keeps a copy in Workspace/Memory-Consolidations.md, and takes the line off this list.`,
      }
    case 'restore':
      return {
        ...base, chat: false, lineRole: 'restore', where: item.qualifier, whereIsNote: false,
        how: `The next Workspace care run adds it back to ${item.qualifier ? `the ${item.qualifier} region` : 'memory'} and takes the line off this list.`,
      }
    case 'category':
      return { ...base, chat: true, how: 'A new category is added in Settings, so Approve opens a chat to settle it.' }
    case 'repo':
      return {
        ...base, chat: true, where: item.qualifier ? `${item.qualifier.replace(/\/$/, '')}/AGENTS.md` : '', whereIsNote: false,
        how: 'This changes a code repository\'s instructions, so Approve opens a chat that applies it.',
      }
    case 'rule':
      return { ...base, chat: true, where: item.qualifier, whereIsNote: false, how: 'This changes a decision rule, so Approve opens a chat that applies it.' }
    case 'observation':
      return {
        ...base, chat: false,
        how: destination
          ? 'The next Workspace care run writes it there and takes the line off this list.'
          : 'The next Workspace care run writes it to your profile or the person\'s note and takes the line off this list.',
      }
    default:
      return {
        ...base, chat: false,
        how: destination
          ? 'The next Workspace care run writes it there and takes the line off this list.'
          : 'The next Workspace care run writes it where it belongs and takes the line off this list. This line does not say where.',
      }
  }
}
