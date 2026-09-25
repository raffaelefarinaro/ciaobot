/** Plain-language reasons for vault-review detection signals, in one place.
 *
 * The panel used to risk interpolating raw signal names (`weak_provenance`)
 * into the UI. A registry keeps the wording consistent and testable without
 * mounting the panel, the same way `proposalKinds.ts` does for proposal rows.
 * Unknown signals (a server newer than the client) fall back to a
 * humanized form rather than disappearing.
 */
import { formatAgeDays } from './relativeTime'
import type { VaultReviewEvidence } from './types'

/** The clause form, for sentences ("Curation flagged it because …"). */
const SIGNAL_LABELS: Record<string, string> = {
  unverified: 'it has gone too long without being checked',
  unlinked: 'no other note links to it',
  possible_duplicate: 'it may duplicate another note',
  superseded_language: 'its wording says it was superseded',
  weak_provenance: 'it carries no date, tags, or aliases',
}

/** The filter-chip form: a short noun phrase per reason. */
const SIGNAL_CHIP_LABELS: Record<string, string> = {
  unverified: 'Unchecked too long',
  superseded_language: 'Says it was superseded',
  unlinked: 'Nothing links to it',
  possible_duplicate: 'Possible duplicate',
  weak_provenance: 'No date or tags',
}

/** The order reasons are shown and filtered in: the ones that most often mean
 * "retire it" first. Unknown signals sort after, alphabetically. */
const SIGNAL_ORDER = ['superseded_language', 'unverified', 'possible_duplicate', 'unlinked', 'weak_provenance']

function humanize(signal: string): string {
  return signal.replace(/_/g, ' ')
}

/** One signal in words the UI can show. */
export function signalLabel(signal: string): string {
  return SIGNAL_LABELS[signal] ?? humanize(signal)
}

/** One signal as a filter chip. */
export function signalChipLabel(signal: string): string {
  const known = SIGNAL_CHIP_LABELS[signal]
  if (known) return known
  const words = humanize(signal)
  return words.charAt(0).toUpperCase() + words.slice(1)
}

/** Signals in display order. */
export function orderedSignals(signals: string[]): string[] {
  const rank = (s: string) => {
    const i = SIGNAL_ORDER.indexOf(s)
    return i === -1 ? SIGNAL_ORDER.length : i
  }
  return [...new Set(signals)].sort((a, b) => rank(a) - rank(b) || a.localeCompare(b))
}

/** Every reason a candidate was flagged, in stable order. */
export function signalReasons(signals: string[]): string[] {
  return [...signals].sort().map(signalLabel)
}

/** An age in days as words: "12 days", "3 months", "2 years". */
export function ageInWords(days: number): string {
  const d = Math.max(0, Math.floor(days))
  if (d < 30) return `${d} ${d === 1 ? 'day' : 'days'}`
  if (d < 365) {
    const months = Math.floor(d / 30)
    return `${months} ${months === 1 ? 'month' : 'months'}`
  }
  const years = Math.floor(d / 365)
  return `${years} ${years === 1 ? 'year' : 'years'}`
}

/** The reason on a row's meta line, lower-case, short enough to sit between
 * the note's type and its backlink count. `unverified` names the age and the
 * limit when the server sent them. */
export function signalRowLabel(signal: string, evidence: VaultReviewEvidence): string {
  if (signal === 'unverified') {
    const u = evidence.unverified
    if (u && Number.isFinite(u.age_days) && Number.isFinite(u.threshold_days)) {
      return `unchecked ${ageInWords(u.age_days)} (limit ${u.threshold_days} days)`
    }
    return 'unchecked too long'
  }
  const chip = SIGNAL_CHIP_LABELS[signal]
  return chip ? chip.charAt(0).toLowerCase() + chip.slice(1) : humanize(signal)
}

/** The last path segment without its extension — the only part that differs
 * between candidate rows. */
export function candidateLeaf(path: string): string {
  const leaf = path.split('/').pop() ?? path
  return leaf.replace(/\.md$/, '') || path
}

/** When the note's facts were last verified, in words.
 *
 * Prefers the server-computed `age_days`; falls back to the raw
 * `last_update` date, which is still more useful than silence.
 */
export function verificationLabel(ageDays: number | null, lastUpdate: string): string {
  if (typeof ageDays === 'number' && Number.isFinite(ageDays)) {
    // Same ladder the Memory Map renders this note's age with, one tab away.
    const age = formatAgeDays(ageDays)
    return age === 'today' ? 'verified today' : `unverified for ${age}`
  }
  return lastUpdate ? `last verified ${lastUpdate}` : 'never verified'
}
