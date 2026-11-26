// Pure helpers for the decision History list (#3056 S4b). No Vue imports, so
// they are tested directly. The engine's history rows carry what each decision
// did (`apply_status`) and who made it (`actor`); rows from an older engine
// carry none of those.

import type { ProposalHistoryActor, ProposalHistoryRow } from './types'

/** Fill the fields an older engine did not send, so the list never renders a blank or NaN. */
export function normaliseHistoryRow(raw: ProposalHistoryRow): ProposalHistoryRow {
  const actor: ProposalHistoryActor | undefined = raw.actor
    ?? (raw.via === 'pwa' ? 'human' : raw.via === 'agent' || raw.via === 'auto' ? 'automated' : undefined)
  const reason = typeof raw.reason === 'string' ? raw.reason.split(/\s+/).filter(Boolean).join(' ') : ''
  return {
    ...raw,
    actor,
    decided_at: raw.decided_at || raw.ts,
    reason: reason || undefined,
  }
}

export interface ApplyLine {
  text: string
  /** `ok` for applied, `bad` for a write that did not land, `muted` otherwise. */
  tone: 'ok' | 'bad' | 'muted'
}

/**
 * What the decision's write did, in words. Null when the engine sent no
 * apply state or says `none`, so the list says nothing rather than guessing.
 * Undone is checked first: an undo wins over the applied row it reverses.
 */
export function applyLine(row: ProposalHistoryRow): ApplyLine | null {
  if (row.receipt_issue === 'missing') {
    return { text: 'Unverified · the receipt for this decision is no longer in the journal', tone: 'muted' }
  }
  if (row.receipt_issue === 'unknown_status') {
    return { text: 'Unverified · the receipt carries a status this version does not know', tone: 'muted' }
  }
  switch (row.apply_status) {
    case 'undone': return { text: 'Undone', tone: 'muted' }
    case 'applied': return { text: 'Applied', tone: 'ok' }
    case 'failed': return { text: 'Not applied · the write failed', tone: 'bad' }
    case 'conflict': return { text: 'Not applied · the destination changed underneath', tone: 'bad' }
    case 'rolled_back': return { text: 'Rolled back', tone: 'muted' }
    case 'pending': return { text: 'Pending · the write has not settled', tone: 'muted' }
    default: return null
  }
}

/** Who decided, in the words the chip uses; null when the engine sent no actor. */
export function historyActorLabel(row: ProposalHistoryRow): string | null {
  if (row.actor === 'human') return 'You'
  if (row.actor === 'automated') return 'Workspace care'
  return null
}

export interface ReasonPreview {
  text: string
  /** Whether the full reason was cut to `text`. */
  long: boolean
}

/** A reason cut at a word boundary once it passes `max` characters. */
export function reasonPreview(reason: string, max = 160): ReasonPreview {
  if (reason.length <= max) return { text: reason, long: false }
  const cut = reason.slice(0, max)
  const space = cut.lastIndexOf(' ')
  const head = (space > 0 ? cut.slice(0, space) : cut).trimEnd()
  return { text: `${head}…`, long: true }
}
