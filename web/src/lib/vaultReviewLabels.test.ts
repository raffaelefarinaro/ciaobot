import { describe, expect, it } from 'vitest'
import { candidateLeaf, signalLabel, signalReasons, verificationLabel } from './vaultReviewLabels'

describe('vault review labels', () => {
  it('names every known detection signal in plain language', () => {
    expect(signalLabel('unlinked')).toBe('no other note links to it')
    expect(signalLabel('possible_duplicate')).toBe('it may duplicate another note')
    expect(signalLabel('superseded_language')).toBe('its wording says it was superseded')
    expect(signalLabel('weak_provenance')).toBe('it carries no date, tags, or aliases')
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
