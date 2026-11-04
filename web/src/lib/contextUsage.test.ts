import { describe, expect, it } from 'vitest'
import { latestContextUsage, parseCompactedStatus, type ContextUsageItem } from './contextUsage'

const assistant = (context_pct: string): ContextUsageItem => ({
  kind: 'assistant',
  meta: { usage: { context_pct } },
})
const compaction: ContextUsageItem = { kind: 'system', msg: { origin: 'compaction' } }
const plainSystem: ContextUsageItem = { kind: 'system', msg: { origin: null } }

describe('parseCompactedStatus', () => {
  it('parses the compact_boundary status with its pre and post token counts', () => {
    expect(parseCompactedStatus('Compacted context: 150,000 → 30,000 tokens')).toEqual({
      done: true,
      tokens: { pre: 150000, post: 30000 },
    })
  })

  it('reports completion without tokens when the status carries none', () => {
    expect(parseCompactedStatus('Compacted context')).toEqual({ done: true, tokens: null })
  })

  it('ignores ticks that are not the completion status', () => {
    expect(parseCompactedStatus('compacting')).toBeNull()
  })
})

describe('latestContextUsage', () => {
  it('returns the last assistant usage when nothing was compacted', () => {
    expect(latestContextUsage([plainSystem, assistant('13.2%')], null)).toEqual({ pct: 13.2, compacted: false })
  })

  it('rescales the usage by post/pre when a compaction is newer', () => {
    const items = [assistant('80%'), compaction]
    expect(latestContextUsage(items, { pre: 200000, post: 40000 })).toEqual({ pct: 16, compacted: true })
  })

  it('hides the figure when a compaction is newer and its token counts are unknown', () => {
    expect(latestContextUsage([assistant('80%'), compaction], null)).toEqual({ pct: null, compacted: true })
  })

  it('ignores an older compaction once a newer usage report exists', () => {
    const items = [compaction, assistant('22%')]
    expect(latestContextUsage(items, { pre: 200000, post: 40000 })).toEqual({ pct: 22, compacted: false })
  })

  it('reports nothing when there are no items', () => {
    expect(latestContextUsage([], null)).toEqual({ pct: null, compacted: false })
  })
})
