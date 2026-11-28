import { describe, expect, it } from 'vitest'
import { isCompactedStatus, latestContextUsage, stampAfter, type ContextUsageItem } from './contextUsage'

const assistant = (usage: Record<string, string>, timestamp = ''): ContextUsageItem => ({
  kind: 'assistant',
  msg: { timestamp },
  meta: { usage },
})
const compaction: ContextUsageItem = { kind: 'system', msg: { origin: 'compaction' } }
const plainSystem: ContextUsageItem = { kind: 'system', msg: { origin: null } }
// The usage an ACP turn carries (claude-agent-acp, captured): tokens and the engine's percentage.
const acpUsage = { input_tokens: '2', output_tokens: '4', context_used: '24992', context_size: '1000000', context_pct: '2.5%' }

describe('isCompactedStatus', () => {
  it('matches the engine status for a finished compaction, not its progress', () => {
    expect(isCompactedStatus('Compacted context')).toBe(true)
    expect(isCompactedStatus('Compacting context')).toBe(false)
  })
})

describe('latestContextUsage', () => {
  it('returns the last assistant usage when nothing was compacted', () => {
    expect(latestContextUsage([plainSystem, assistant({ context_pct: '13.2%' })])).toEqual({
      pct: 13.2, compacted: false, used: null, size: null,
    })
  })

  it('reads the tokens behind an ACP usage report', () => {
    expect(latestContextUsage([assistant(acpUsage)])).toEqual({ pct: 2.5, compacted: false, used: 24992, size: 1000000 })
  })

  it('hides the figure when a compaction is newer than the last usage', () => {
    expect(latestContextUsage([assistant({ context_pct: '80%' }), compaction])).toEqual({
      pct: null, compacted: true, used: null, size: null,
    })
  })

  it('ignores an older compaction once a newer usage report exists', () => {
    expect(latestContextUsage([compaction, assistant({ context_pct: '22%' })]).pct).toBe(22)
  })

  it('reports nothing when there are no items', () => {
    expect(latestContextUsage([])).toEqual({ pct: null, compacted: false, used: null, size: null })
  })

  it('prefers a live report newer than the last reply (a compaction turn leaves no reply)', () => {
    const items = [assistant({ context_pct: '40%' }, '2026-11-28T10:00:00Z')]
    const live = { usage: { context_used: '10714', context_size: '1000000', context_pct: '1.1%' }, at: '2026-11-28T10:05:00Z' }
    expect(latestContextUsage(items, live)).toEqual({ pct: 1.1, compacted: false, used: 10714, size: 1000000 })
  })

  it('shows a compaction with no new figure yet as compacted, with the meter hidden', () => {
    const items = [assistant({ context_pct: '40%' }, '2026-11-28T10:00:00Z')]
    expect(latestContextUsage(items, { usage: null, at: '2026-11-28T10:05:00Z' })).toEqual({
      pct: null, compacted: true, used: null, size: null,
    })
  })

  it('prefers a reply newer than the live report', () => {
    const items = [assistant({ context_pct: '12%' }, '2026-11-28T11:00:00Z')]
    expect(latestContextUsage(items, { usage: null, at: '2026-11-28T10:05:00Z' }).pct).toBe(12)
  })
})

describe('stampAfter', () => {
  it('dates a compaction after the newest reply even when the browser clock runs behind the server', () => {
    const reply = { timestamp: '2026-11-28T10:00:00.000Z' }
    const behind = Date.parse('2026-11-28T09:55:00Z')
    const at = stampAfter([{ timestamp: '' }, reply], behind)
    expect(at).toBe('2026-11-28T10:00:00.001Z')
    // So the placeholder, not the pre-compaction reply, decides the meter.
    const items = [assistant({ context_pct: '40%' }, reply.timestamp)]
    expect(latestContextUsage(items, { usage: null, at })).toMatchObject({ pct: null, compacted: true })
  })

  it('uses the browser clock when it is ahead of every message', () => {
    expect(stampAfter([{ timestamp: '2026-11-28T10:00:00Z' }], Date.parse('2026-11-28T11:00:00Z'))).toBe('2026-11-28T11:00:00.000Z')
  })
})
