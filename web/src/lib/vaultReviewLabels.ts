/** Plain-language reasons for vault-review detection signals, in one place.
 *
 * The panel used to risk interpolating raw signal names (`weak_provenance`)
 * into the UI. A registry keeps the wording consistent and testable without
 * mounting the panel, the same way `proposalKinds.ts` does for proposal rows.
 * Unknown signals (a server newer than the client) fall back to a
 * humanized form rather than disappearing.
 */

const SIGNAL_LABELS: Record<string, string> = {
  unlinked: 'no other note links to it',
  possible_duplicate: 'it may duplicate another note',
  superseded_language: 'its wording says it was superseded',
  weak_provenance: 'it carries no date, tags, or aliases',
}

/** One signal in words the UI can show. */
export function signalLabel(signal: string): string {
  return SIGNAL_LABELS[signal] ?? signal.replace(/_/g, ' ')
}

/** Every reason a candidate was flagged, in stable order. */
export function signalReasons(signals: string[]): string[] {
  return [...signals].sort().map(signalLabel)
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
    if (ageDays < 1) return 'verified today'
    if (ageDays < 30) return `unverified for ${ageDays}d`
    if (ageDays < 365) return `unverified for ${Math.floor(ageDays / 30)}mo`
    const years = ageDays / 365
    return `unverified for ${years >= 2 ? Math.floor(years) : years.toFixed(1)}y`
  }
  return lastUpdate ? `last verified ${lastUpdate}` : 'never verified'
}
