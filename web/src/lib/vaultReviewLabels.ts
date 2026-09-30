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
  // A note can be current and still be holding a fact nobody has checked: the
  // note's own date is one date for every bullet in it, so re-checking the
  // address last week silently re-certified the rest of the file with it. This
  // is the signal for the bullets, and it is why the word says "inside".
  unverified_entries: 'facts inside it have gone unchecked',
  unlinked: 'no other note links to it',
  possible_duplicate: 'it may duplicate another note',
  superseded_language: 'its wording says it was superseded',
  weak_provenance: 'it carries no date, tags, or aliases',
}

/** The filter-chip form: a short noun phrase per reason. */
const SIGNAL_CHIP_LABELS: Record<string, string> = {
  unverified: 'Unchecked too long',
  unverified_entries: 'Facts unchecked inside',
  superseded_language: 'Says it was superseded',
  unlinked: 'Nothing links to it',
  possible_duplicate: 'Possible duplicate',
  weak_provenance: 'No date or tags',
}

/** The order reasons are shown and filtered in: the ones that most often mean
 * "retire it" first. Unknown signals sort after, alphabetically. */
const SIGNAL_ORDER = [
  'superseded_language',
  'unverified',
  'unverified_entries',
  'possible_duplicate',
  'unlinked',
  'weak_provenance',
]

/** Why an *entry* was selected, in words. A row that showed only the code would
 * leave a reader unable to tell "nobody ever checked this bullet" from "the
 * last check is two years old" — two different amounts of work. */
const ENTRY_REASON_WORDS: Record<string, string> = {
  aged: 'last checked too long ago',
  'no-stamp': 'never checked',
  'unusable-stamp': 'the check on it is not a usable date',
}

/** One entry reason, in the panel's own words. */
export function entryReasonLabel(reason: string): string {
  return ENTRY_REASON_WORDS[reason] || reason || 'needs a check'
}

/** `2 blocks`, `1 block` — a count with its noun agreeing, for a sentence a
 * person reads rather than a table. */
function plural(count: number, singular: string, plural_?: string): string {
  return `${count} ${count === 1 ? singular : (plural_ ?? `${singular}s`)}`
}

/** One note's facts, counted in words.
 *
 * The number is a share of the note's characters, and a note is mostly
 * frontmatter, headings and blank lines — so a low share is normal and saying
 * "12% covered" for a perfectly well-kept note would be alarm. What matters is
 * whether the *assertions* were read, which is what `uncovered` counts: prose
 * paragraphs, tables and quotes are assertions nothing here checked, and one is
 * enough to stop the note being called fully verified.
 *
 * `unverified` is deliberately one bucket, so the sentence says "not verified"
 * rather than "never checked": it covers a bullet with no stamp *and* one
 * carrying a stamp that cannot be read, and only the per-card copy below the
 * list can tell those apart. Calling the second "never checked" would put a
 * claim on the card that its own excerpt contradicts.
 */
export function coverageSummary(evidence: VaultReviewEvidence): string {
  const cov = evidence.entry_verification
  if (!cov) return ''
  const facts = cov.checked + cov.exempt
  if (!facts) {
    return cov.uncovered
      ? `No facts written as list items — ${plural(cov.uncovered, 'block')} of prose this check could not read`
      : 'No facts in this note to check'
  }
  const parts = [`${plural(cov.entries, 'fact')} in the note`]
  if (cov.stale) parts.push(`${cov.stale} past due`)
  if (cov.unverified) parts.push(`${cov.unverified} not verified`)
  if (cov.exempt) parts.push(`${cov.exempt} recorded as events`)
  if (cov.uncovered) parts.push(`${plural(cov.uncovered, 'block')} of prose not read as facts`)
  return parts.join(' · ')
}

/** What a verification's `outcome` verdict was, in the pass's own words.
 *
 * These are claims about a note, not about the operation that checked it: an
 * `unverified` outcome is a real answer ("I looked and could not confirm it"),
 * not a failure, and the word is shared with the operation's own status for
 * exactly that reason. Rendered verbatim rather than translated, so a reader can
 * match what the panel says against what the agent reported. */
const VERDICT_WORDS: Record<string, string> = {
  still_valid: 'the note is still true',
  update: 'the note needs updating',
  retire: 'the note should be retired',
  unverified: 'could not be confirmed',
}

/** One verdict, in words. An outcome this client has not heard of is shown
 * as-is rather than hidden, since the server's vocabulary is the authority. */
export function verdictLabel(outcome: string): string {
  return VERDICT_WORDS[outcome] || outcome || 'not recorded'
}

/** How much of a note a check actually covered, in words.
 *
 * `partial` matters more than it looks: a re-stamp claims the WHOLE note is
 * still true, so the pass only auto-applies one from `complete` coverage, and a
 * half-checked note is recorded as unverified and asked again. A row that said
 * "checked" without this would claim more than the pass did. */
export function coverageLabel(coverage: string): string {
  if (coverage === 'complete') return 'the whole note'
  if (coverage === 'partial') return 'part of the note'
  return 'an unstated amount of the note'
}

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
  if (signal === 'unverified_entries') {
    const cov = evidence.entry_verification
    const due = cov ? cov.stale + cov.unverified : 0
    return due ? `${plural(due, 'fact')} inside unchecked` : 'facts inside unchecked'
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

/**
 * The completion refusals `complete_project_note` raises, in the engine's own
 * words, each with what the operator should do instead.
 *
 * The row is decided server-side, so a refusal usually means the row was stale —
 * and its message reached the error toast verbatim: "only a project can be
 * completed; retire this note instead" is a sentence written for a log reader,
 * not for someone who just clicked Complete on a project. Matched by prefix
 * because several of them carry the offending path behind them.
 */
const COMPLETION_REFUSALS: ReadonlyArray<readonly [string, string]> = [
  [
    'only a project can be completed',
    'This is not a project, so there is nothing to complete. Retire moves it to Retired instead.',
  ],
  [
    'note is outside the vault',
    'That note is no longer inside this workspace’s vault.',
  ],
  [
    'candidate changed or no longer exists',
    'This note changed since this list was built. The list has been refreshed — try again.',
  ],
  [
    'this note has no projects/ layout',
    'This note has no projects/ layout, so there is nowhere to complete it into.',
  ],
  [
    'completion path is outside the vault',
    'Completing this note would land outside the vault, so the move was refused.',
  ],
  [
    'completion destination is outside the vault',
    'Completing this note would land outside the vault, so the move was refused.',
  ],
  [
    'a note already exists at',
    'Something already sits at the completed path for this project, so the move was refused.',
  ],
]

/**
 * Plain copy for a completion refusal, or '' when the message is not one.
 *
 * An empty answer is the important half: anything the engine says that this
 * table does not recognise — a genuine 500, a message from a newer engine —
 * keeps its own wording rather than being replaced by a confident guess about
 * a failure nobody has described here.
 */
export function completionRefusalCopy(message: string): string {
  const hit = COMPLETION_REFUSALS.find(([prefix]) => message.includes(prefix))
  return hit ? hit[1] : ''
}
