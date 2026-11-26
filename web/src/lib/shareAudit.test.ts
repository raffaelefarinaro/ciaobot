import { describe, expect, it } from 'vitest'
import { audienceLines, auditEvents, clipText, grantStatusLabel, retentionText } from './shareAudit'
import type { ShareEvidenceGrant } from './types'

function grant(over: Partial<ShareEvidenceGrant> = {}): ShareEvidenceGrant {
  return {
    grant_id: 'grn_a',
    type: 'standing',
    project_id: 'proj',
    source_id: 'stream:proj',
    source_kind: 'stream',
    source_label: 'Structured decisions',
    brain: { name: 'team-notes', identity: 'github.com/acme/team-notes', link_fingerprint: 'f' },
    audience: 'github.com/acme/team-notes',
    field_mask: ['decision'],
    producers: ['memory_extraction'],
    issued_at: '2026-11-25T10:00:00Z',
    start_revision: 1,
    consent_generation: 1,
    active: true,
    linked: true,
    status: 'active',
    revoked_at: null,
    revoked_generation: null,
    withdrawn_at: null,
    withdrawal_reason: '',
    history: [{ event: 'issued', at: '2026-11-25T10:00:00Z', generation: 1 }],
    ...over,
  }
}

describe('shareAudit', () => {
  it('never presents an address as a known audience', () => {
    const lines = audienceLines('github.com/acme/team-notes')
    expect(lines[0]).toContain('Audience unknown')
    expect(lines[1]).toContain('Target: github.com/acme/team-notes')
    expect(audienceLines('')).toHaveLength(1)
  })

  it('does not claim recall or cloud sync', () => {
    expect(retentionText('git')).toContain('cannot recall')
    expect(retentionText('folder')).toContain('does not confirm')
    expect(retentionText('folder')).not.toMatch(/cannot recall/)
  })

  it('names every grant status in words', () => {
    expect(['active', 'revoked', 'withdrawn', 'unlinked', 'inactive'].map((status) =>
      grantStatusLabel({ status } as Pick<ShareEvidenceGrant, 'status'>))).toEqual([
      'Recorded, not yet in use', 'Revoked', 'Withdrawn', 'Brain unlinked', 'Inactive',
    ])
  })

  it('merges history across grants newest first and tolerates bad rows', () => {
    const a = grant({
      history: [
        { event: 'issued', at: '2026-11-25T10:00:00Z', generation: 1 },
        { event: 'revoked', at: '2026-11-25T12:00:00Z', generation: 1 },
      ],
    })
    const b = grant({
      grant_id: 'grn_b',
      type: 'snapshot',
      history: [
        { event: 'issued', at: '2026-11-25T11:00:00Z', generation: 1 },
        { event: 'withdrawn', at: '2026-11-25T13:00:00Z', generation: 1, reason: 'superseded' },
        { event: 'mystery', at: 'not a date', secret: 'do not show' },
        null as unknown as Record<string, unknown>,
      ],
    })
    const events = auditEvents([a, b])
    expect(events.map((e) => e.text)).toEqual([
      'Withdrawn (generation 1): superseded',
      'Revoked (generation 1)',
      'Permission recorded (generation 1)',
      'Permission recorded (generation 1)',
      'Change recorded',
    ])
    expect(events.map((e) => e.grantId)).toEqual(['grn_b', 'grn_a', 'grn_b', 'grn_a', 'grn_b'])
    expect(events[4].when).toBe('Time unknown')
    expect(JSON.stringify(events)).not.toContain('do not show')
  })

  it('clips long text with an ellipsis and leaves short text alone', () => {
    expect(clipText('abc')).toBe('abc')
    expect(clipText('x'.repeat(500))).toHaveLength(201)
    expect(clipText('x'.repeat(500), 80).endsWith('…')).toBe(true)
  })

  it('bounds an engine-recorded reason', () => {
    const [event] = auditEvents([grant({ history: [{ event: 'withdrawn', at: '2026-11-25T10:00:00Z', reason: 'x'.repeat(500) }] })])
    expect(event.text.length).toBeLessThan(260)
  })
})
