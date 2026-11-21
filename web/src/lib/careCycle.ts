// Pure helpers for the Settings "Workspace care cycle" card. The engine owns the
// preview and the switch (`/api/care/cycle`); this module only decides which
// state the card is in and what plain sentences it shows. No Vue imports.
import type { CareCycleHostResponse, CareCycleReport, CareCycleResponse } from './types'

/** The vault-relative backup written before the state file is replaced. */
export const CARE_CYCLE_BACKUP_RELATIVE = 'Workspace/Curation-State.v1.bak'

export const CARE_CYCLE_LEASE_SENTENCE = 'A care run is in progress. Wait for it to finish.'

/** Shown to a browser that is not on the engine's computer: a copyable CLI line, never a button. */
export const CARE_CYCLE_CLI_COMMAND = 'ciao curation-cycle-migrate --vault-root <your vault folder> --dry-run'

export const CARE_CYCLE_WARNING =
  'Stop other copies first. Any other Ciaobot version that points at this workspace will refuse its care runs until it is updated. This does not touch your notes.'

export const CARE_CYCLE_CONFIRM_LABEL =
  'I have stopped every other Ciaobot or older version that uses this workspace.'

export type CareCycleView =
  | { kind: 'not-on-host' }
  | { kind: 'switched'; report: CareCycleReport }
  | { kind: 'blocked'; report: CareCycleReport; reasons: string[]; running: boolean; unreadable: boolean }
  | { kind: 'ready'; report: CareCycleReport }

function isLiveLeaseBlocker(text: string): boolean {
  return text.includes('live curation lease')
}

/**
 * The plain sentences that stop a switch. A live lease becomes the one
 * sentence about a care run in progress; the engine's other blockers pass through.
 */
export function blockerSentences(report: CareCycleReport): string[] {
  const reasons: string[] = []
  if (report.live_lease) reasons.push(CARE_CYCLE_LEASE_SENTENCE)
  for (const blocker of report.blockers) {
    if (isLiveLeaseBlocker(blocker)) continue
    if (!reasons.includes(blocker)) reasons.push(blocker)
  }
  return reasons
}

/** Which of the card's states a dry-run answer puts it in. */
export function careCycleView(res: CareCycleResponse): CareCycleView {
  if (!res.on_host) return { kind: 'not-on-host' }
  const report = res as CareCycleHostResponse
  if (report.status === 'v2') return { kind: 'switched', report }
  const reasons = blockerSentences(report)
  if (reasons.length > 0) {
    return {
      kind: 'blocked',
      report,
      reasons,
      running: report.lease_live,
      unreadable: report.status === 'unreadable',
    }
  }
  return { kind: 'ready', report }
}

function count(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`
}

/** What a switch keeps, in the order the card lists it. Counts only; no keys or paths. */
export function readySummary(report: CareCycleReport): string[] {
  const lines = [
    `${count(report.daily, 'daily progress record', 'daily progress records')} kept.`,
    `${count(report.weekly_historical, 'weekly record', 'weekly records')} kept as history. They need fresh proof before they count again.`,
  ]
  if (report.ambiguous.length > 0) {
    const one = report.ambiguous.length === 1
    lines.push(`${count(report.ambiguous.length, 'weekly record', 'weekly records')} ${one ? 'is' : 'are'} ambiguous and ${one ? 'needs' : 'need'} fresh proof.`)
  }
  if (report.dropped.length > 0) {
    lines.push(`${count(report.dropped.length, 'daily record', 'daily records')} dated after today ${report.dropped.length === 1 ? 'is' : 'are'} dropped.`)
  }
  return lines
}

/** The reversal instructions the card states. Ciaobot does not undo the switch itself. */
export function undoSentence(): string {
  return `A copy of the current state is saved as ${CARE_CYCLE_BACKUP_RELATIVE} in this workspace's vault. Ciaobot does not undo this automatically. To go back: quit Ciaobot, replace Curation-State.json with that copy; progress made after the switch is not carried back.`
}
