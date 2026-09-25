import { describe, expect, it } from 'vitest'
import {
  ageInWords, candidateLeaf, orderedSignals, signalChipLabel, signalLabel, signalReasons, signalRowLabel, verificationLabel,
} from './vaultReviewLabels'
import type { VaultReviewEvidence } from './types'
import { formatAgeDays } from './relativeTime'

describe('vault review labels', () => {
  it('names every known detection signal in plain language', () => {
    expect(signalLabel('unlinked')).toBe('no other note links to it')
    expect(signalLabel('possible_duplicate')).toBe('it may duplicate another note')
    expect(signalLabel('superseded_language')).toBe('its wording says it was superseded')
    expect(signalLabel('weak_provenance')).toBe('it carries no date, tags, or aliases')
  })

  it('names the unverified signal and every chip', () => {
    expect(signalLabel('unverified')).toBe('it has gone too long without being checked')
    expect(signalChipLabel('unverified')).toBe('Unchecked too long')
    expect(signalChipLabel('superseded_language')).toBe('Says it was superseded')
    expect(signalChipLabel('unlinked')).toBe('Nothing links to it')
    expect(signalChipLabel('possible_duplicate')).toBe('Possible duplicate')
    expect(signalChipLabel('weak_provenance')).toBe('No date or tags')
    expect(signalChipLabel('stale_horizon')).toBe('Stale horizon')
  })

  it('orders signals by how often they mean retire', () => {
    expect(orderedSignals(['weak_provenance', 'unlinked', 'unverified', 'superseded_language', 'zeta']))
      .toEqual(['superseded_language', 'unverified', 'unlinked', 'weak_provenance', 'zeta'])
  })

  it('puts the age and the limit on an unverified row, and degrades without them', () => {
    const base: VaultReviewEvidence = {
      backlinks: [], outbound_links: [], bridge: false, duplicate_group: [], last_update: '', type: 'person', age_days: null,
    }
    expect(signalRowLabel('unverified', {
      ...base,
      unverified: { age_days: 95, threshold_days: 90, last_verified: '2026-06-22', source: 'frontmatter' },
    })).toBe('unchecked 3 months (limit 90 days)')
    expect(signalRowLabel('unverified', base)).toBe('unchecked too long')
    expect(signalRowLabel('superseded_language', base)).toBe('says it was superseded')
    expect(signalRowLabel('stale_horizon', base)).toBe('stale horizon')
  })

  it('says ages in words', () => {
    expect(ageInWords(1)).toBe('1 day')
    expect(ageInWords(12)).toBe('12 days')
    expect(ageInWords(31)).toBe('1 month')
    expect(ageInWords(95)).toBe('3 months')
    expect(ageInWords(800)).toBe('2 years')
  })

  it('humanizes an unknown signal instead of dropping it', () => {
    expect(signalLabel('stale_horizon')).toBe('stale horizon')
  })

  it('orders reasons stably regardless of server order', () => {
    expect(signalReasons(['weak_provenance', 'unlinked'])).toEqual([
      'no other note links to it',
      'it carries no date, tags, or aliases',
    ])
  })

  it('reads the note title off the path leaf', () => {
    expect(candidateLeaf('memory-vault/People/Mo.md')).toBe('Mo')
    expect(candidateLeaf('Mo.md')).toBe('Mo')
  })

  it('phrases verification age like the memory map does', () => {
    expect(verificationLabel(0, '')).toBe('verified today')
    expect(verificationLabel(5, '')).toBe('unverified for 5d')
    expect(verificationLabel(65, '')).toBe('unverified for 2mo')
    expect(verificationLabel(null, '2025-01-01')).toBe('last verified 2025-01-01')
    expect(verificationLabel(null, '')).toBe('never verified')
  })
})

describe('the age ladder is shared with the Memory Map', () => {
  // The retirement row and the Memory Map's detail panel describe the SAME
  // note, one tab apart. They used to carry independent copies of these
  // thresholds, so a change to either left them disagreeing about it.
  it('reads its buckets straight from formatAgeDays', () => {
    for (const days of [0, 0.5, 1, 29, 30, 200, 364, 365, 400, 730, 1200]) {
      const age = formatAgeDays(days)
      const expected = age === 'today' ? 'verified today' : `unverified for ${age}`
      expect(verificationLabel(days, '')).toBe(expected)
    }
  })

  it('still falls back to the raw date, then to silence', () => {
    expect(verificationLabel(null, '2026-01-05')).toBe('last verified 2026-01-05')
    expect(verificationLabel(null, '')).toBe('never verified')
    expect(verificationLabel(Number.NaN, '')).toBe('never verified')
  })
})
