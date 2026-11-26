// Pure helpers for the read-only Settings "Consent and history" view. No Vue imports.
import type { ShareEvidenceGrant } from './types'

/** Plain words for a grant's status; the state never rests on colour alone. */
export function grantStatusLabel(grant: Pick<ShareEvidenceGrant, 'status'>): string {
  if (grant.status === 'active') return 'Active'
  if (grant.status === 'unlinked') return 'Brain unlinked'
  if (grant.status === 'withdrawn') return 'Withdrawn'
  if (grant.status === 'revoked') return 'Revoked'
  return 'Inactive'
}

const PRODUCER_LABEL: Record<string, string> = {
  memory_extraction: 'Memory extraction',
  nightly_curation: 'Nightly curation',
}

export function producerLabel(producer: string): string {
  return PRODUCER_LABEL[producer] ?? producer
}

/**
 * Who could read a note is not known: a repository address does not show who
 * has access today. The address is shown as the target, never as the audience.
 */
export function audienceLines(target: string): string[] {
  const lines = ['Audience unknown. Readers and access have not been verified.']
  if (target) lines.push(`Target: ${target}. An address does not show who can read it.`)
  return lines
}

/** What leaving the workspace means for a published note, by link kind. */
export function retentionText(kind: string): string {
  return kind === 'folder'
    ? 'Saved as a file in the folder on this computer. This does not confirm that Google Drive, iCloud or another sync service has finished syncing.'
    : "Published notes stay in the repository's history. Ciaobot cannot recall a note once it is published."
}

export function localTime(at: string): string {
  const when = new Date(at)
  return at && !Number.isNaN(when.getTime()) ? when.toLocaleString() : 'Time unknown'
}

export interface AuditEvent {
  key: string
  at: string
  when: string
  text: string
  grantId: string
  grantLabel: string
}

function eventText(row: Record<string, unknown>): string {
  const generation = typeof row.generation === 'number' ? ` (generation ${row.generation})` : ''
  const reason = typeof row.reason === 'string' && row.reason.trim() ? `: ${row.reason.trim().slice(0, 200)}` : ''
  if (row.event === 'issued') return `Granted${generation}`
  if (row.event === 'revoked') return `Revoked${generation}`
  if (row.event === 'withdrawn') return `Withdrawn${generation}${reason}`
  return 'Change recorded'
}

/** One line for the grant a history row belongs to. */
export function grantLabel(grant: ShareEvidenceGrant): string {
  const what = grant.type === 'standing' ? 'Future decisions' : 'One decision'
  return `${what} to ${grant.brain.name || 'unknown brain'}`
}

/** Every grant event in one list, newest first; rows with no usable time sort last. */
export function auditEvents(grants: ShareEvidenceGrant[]): AuditEvent[] {
  const rows: Array<AuditEvent & { stamp: number; order: number }> = []
  for (const grant of grants) {
    for (const raw of grant.history ?? []) {
      if (!raw || typeof raw !== 'object') continue
      const at = typeof raw.at === 'string' ? raw.at : ''
      const stamp = Date.parse(at)
      rows.push({
        key: `${grant.grant_id}:${rows.length}`,
        at,
        when: localTime(at),
        text: eventText(raw),
        grantId: grant.grant_id,
        grantLabel: grantLabel(grant),
        stamp: Number.isNaN(stamp) ? Number.NEGATIVE_INFINITY : stamp,
        order: rows.length,
      })
    }
  }
  rows.sort((a, b) => (b.stamp === a.stamp ? b.order - a.order : b.stamp > a.stamp ? 1 : -1))
  return rows.map(({ stamp: _stamp, order: _order, ...event }) => event)
}
