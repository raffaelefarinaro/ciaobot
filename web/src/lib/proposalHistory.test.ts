import { describe, expect, it } from 'vitest'
import {
  applyLine,
  careRunScheduleIndex,
  historyActorLabel,
  normaliseHistoryRow,
  reasonPreview,
} from './proposalHistory'
import type { ProposalHistoryRow } from './types'

function row(overrides: Partial<ProposalHistoryRow> = {}): ProposalHistoryRow {
  return {
    id: 'h1',
    ts: '2026-09-01T10:00:00+00:00',
    action: 'accepted',
    via: 'pwa',
    kind: 'memory',
    text: 'Remember the thing',
    source: '',
    workspace: 'personal',
    destination: 'ciao:memory',
    outcome: 'written',
    proposal_id: 'p1',
    ...overrides,
  }
}

describe('normaliseHistoryRow', () => {
  it('derives actor and decided_at for a row from an engine that predates them', () => {
    const out = normaliseHistoryRow(row({ via: 'pwa' }))
    expect(out.actor).toBe('human')
    expect(out.decided_at).toBe('2026-09-01T10:00:00+00:00')
    expect(out.apply_status).toBeUndefined()
    expect(out.applied).toBeUndefined()
  })

  it('treats agent and auto decisions as automated when the engine gave no actor', () => {
    expect(normaliseHistoryRow(row({ via: 'agent' })).actor).toBe('automated')
    expect(normaliseHistoryRow(row({ via: 'auto' })).actor).toBe('automated')
  })

  it('leaves no actor when neither the engine nor the via tells who decided', () => {
    expect(normaliseHistoryRow(row({ via: '' })).actor).toBeUndefined()
  })

  it('keeps the engine actor and decided_at when present', () => {
    const out = normaliseHistoryRow(row({
      via: 'agent', actor: 'human', decided_at: '2026-09-02T08:00:00+00:00',
    }))
    expect(out.actor).toBe('human')
    expect(out.decided_at).toBe('2026-09-02T08:00:00+00:00')
  })

  it('drops an empty or whitespace reason so no blank line renders', () => {
    expect(normaliseHistoryRow(row({ reason: '   ' })).reason).toBeUndefined()
    expect(normaliseHistoryRow(row({ reason: ' why\n  now ' })).reason).toBe('why now')
  })
})

describe('applyLine', () => {
  it('says Applied for an applied write', () => {
    expect(applyLine(row({ apply_status: 'applied', applied: true }))).toEqual({ text: 'Applied', tone: 'ok' })
  })

  it('says Not applied, never Applied, for an approval whose write failed', () => {
    const line = applyLine(row({ apply_status: 'failed', applied: false }))
    expect(line?.text.startsWith('Not applied')).toBe(true)
    expect(line?.tone).toBe('bad')
  })

  it('says Not applied for a conflict', () => {
    expect(applyLine(row({ apply_status: 'conflict', applied: false }))?.text.startsWith('Not applied')).toBe(true)
  })

  it('says Undone, and Undone wins even when the row still reads applied', () => {
    expect(applyLine(row({ apply_status: 'undone', applied: false }))?.text).toBe('Undone')
    expect(applyLine(row({ apply_status: 'undone', applied: true }))?.text).toBe('Undone')
  })

  it('says Rolled back and Pending', () => {
    expect(applyLine(row({ apply_status: 'rolled_back', applied: false }))?.text).toBe('Rolled back')
    expect(applyLine(row({ apply_status: 'pending', applied: null }))?.text.startsWith('Pending')).toBe(true)
  })

  it('says Unverified, never Applied, when the named receipt is missing or unreadable', () => {
    const missing = applyLine(row({ apply_status: 'none', applied: null, receipt_issue: 'missing' }))
    expect(missing?.tone).toBe('bad')
    expect(missing?.text).toBe('Unverified · the receipt for this decision is no longer in the journal')
    const unknown = applyLine(row({ apply_status: 'none', applied: null, receipt_issue: 'unknown_status' }))
    expect(unknown?.text.startsWith('Unverified')).toBe(true)
  })

  it('says nothing for none, or for a row from an engine without apply_status', () => {
    expect(applyLine(row({ apply_status: 'none', applied: null }))).toBeNull()
    expect(applyLine(row())).toBeNull()
  })
})

describe('historyActorLabel', () => {
  it('says You for a human decision and Workspace care for an automated one', () => {
    expect(historyActorLabel(row({ actor: 'human' }))).toBe('You')
    expect(historyActorLabel(row({ actor: 'automated' }))).toBe('Workspace care')
  })

  it('is null when the engine sent no actor, so the caller keeps its existing label', () => {
    expect(historyActorLabel(row())).toBeNull()
  })
})

describe('reasonPreview', () => {
  it('returns a short reason whole and not long', () => {
    expect(reasonPreview('short why')).toEqual({ text: 'short why', long: false })
  })

  it('cuts a long reason at a word boundary with an ellipsis and reports it long', () => {
    const reason = 'word '.repeat(80).trim()
    const out = reasonPreview(reason, 40)
    expect(out.long).toBe(true)
    expect(out.text.endsWith('…')).toBe(true)
    expect(out.text.length).toBeLessThanOrEqual(41)
    expect(out.text.slice(0, -1).endsWith(' ')).toBe(false)
  })
})

describe('careRunScheduleIndex', () => {
  const schedule = (id: string, chat: string | undefined) => ({ schedule_id: id, last_run_chat_id: chat })

  it('maps a run id to the care schedule whose last run it is, and skips the rest', async () => {
    const seen: string[] = []
    const index = await careRunScheduleIndex(
      [
        schedule('system-memory-curation', 'chat_a'),
        schedule('system-memory-curation@work', 'chat_b'),
        schedule('daily-news', 'chat_c'),
        schedule('system-memory-curation@idle', undefined),
      ],
      async id => {
        seen.push(id)
        if (id === 'system-memory-curation') return { run_id: 'run_1', planned: 1, completed: 1, deferred: 0, failed: 0, counts_complete: true, reasons: [] }
        if (id === 'system-memory-curation@work') return { run_id: 'run_2', planned: 0, completed: 0, deferred: 0, failed: 0, counts_complete: true, reasons: [] }
        return null
      },
    )
    expect(seen).toEqual(['system-memory-curation', 'system-memory-curation@work'])
    expect(index.get('run_1')).toBe('system-memory-curation')
    expect(index.get('run_2')).toBe('system-memory-curation@work')
    expect(index.size).toBe(2)
  })

  it('resolves to nothing when a lookup fails, rather than guessing', async () => {
    const index = await careRunScheduleIndex(
      [schedule('system-memory-curation', 'chat_a')],
      async () => { throw new Error('boom') },
    )
    expect(index.size).toBe(0)
  })
})
