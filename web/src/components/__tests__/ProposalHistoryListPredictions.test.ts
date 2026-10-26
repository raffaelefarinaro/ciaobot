// @vitest-environment jsdom
//
// History rows that carry the nightly run's prediction, and rows the run
// settled itself. A miss is information, never an error, and Undo on a
// care-settled row is offered only where a receipt can honour it.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import ProposalHistoryList from '../ProposalHistoryList.vue'
import type { ProposalHistoryRow } from '../../lib/types'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())

vi.mock('../../lib/api', () => ({
  api: { get: apiGet, post: apiPost, patch: vi.fn(), del: vi.fn() },
}))

function historyRow(overrides: Partial<ProposalHistoryRow> = {}): ProposalHistoryRow {
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

const CHANGE = {
  receipt_id: 'mrcpt_1', kind: 'memory', status: 'applied',
  destination: 'ciao:memory', undoable: true, changed: true, ts: '2026-09-01T09:59:00+00:00',
}

async function mountRows(rows: ProposalHistoryRow[]) {
  apiGet.mockResolvedValue({ rows, total: rows.length, truncated: false })
  const wrapper = mount(ProposalHistoryList, { global: { plugins: [createPinia()] } })
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  setActivePinia(createPinia())
  apiGet.mockReset()
  apiPost.mockReset()
})

afterEach(() => {
  document.body.innerHTML = ''
})

describe('History predictions', () => {
  it('shows predicted vs actual with a match badge for a pwa-decided region row', async () => {
    const view = await mountRows([historyRow({
      prediction: { verdict: 'accept', reason: 'a durable preference', ts: 't', match: true },
    })])
    const line = view.find('.ph-prediction')
    expect(line.text()).toContain('Predicted accept, accepted')
    expect(line.text()).toContain('a durable preference')
    expect(line.find('.badge').text()).toBe('Matched')
    expect(line.find('.badge').classes()).toContain('badge--success')
  })

  it('shows a miss as muted information, not an error', async () => {
    const view = await mountRows([historyRow({
      action: 'dismissed',
      prediction: { verdict: 'accept', reason: 'looked durable', ts: 't', match: false },
    })])
    const badge = view.find('.ph-prediction .badge')
    expect(view.find('.ph-prediction').text()).toContain('Predicted accept, dismissed')
    expect(badge.text()).toBe('Missed')
    expect(badge.classes()).toContain('badge--muted')
    expect(badge.classes().join(' ')).not.toMatch(/error|danger/)
  })

  it('renders the prediction reason as text, never markup', async () => {
    const view = await mountRows([historyRow({
      prediction: { verdict: 'accept', reason: '<img src=x onerror=1>', ts: 't', match: true },
    })])
    expect(view.find('.ph-prediction img').exists()).toBe(false)
    expect(view.find('.ph-prediction-reason').text()).toBe('<img src=x onerror=1>')
  })

  it('shows no prediction line for a row without one', async () => {
    const view = await mountRows([historyRow()])
    expect(view.find('.ph-prediction').exists()).toBe(false)
  })
})

describe('History rows settled by Workspace care', () => {
  it('labels an agent-settled accepted region row "Filed by Workspace care"', async () => {
    const view = await mountRows([historyRow({ via: 'agent' })])
    expect(view.find('.ph-actor--care').text()).toBe('Filed by Workspace care')
  })

  it('labels an agent-dismissed region row "Dismissed by Workspace care"', async () => {
    const view = await mountRows([historyRow({ via: 'agent', action: 'dismissed', kind: 'profile' })])
    expect(view.find('.ph-actor--care').text()).toBe('Dismissed by Workspace care')
  })

  it('does not use the care label for a non-region agent row', async () => {
    const view = await mountRows([historyRow({ via: 'agent', kind: 'learnings' })])
    expect(view.find('.ph-actor--care').exists()).toBe(false)
    expect(view.find('.ph-actor').text()).toBe('agent')
  })

  it('offers a row-level Undo for a care-settled row and calls undoReceipt', async () => {
    const view = await mountRows([historyRow({ via: 'agent', change: CHANGE })])
    apiPost.mockResolvedValue({})
    await view.find('.ph-care-undo').trigger('click')
    expect(apiPost).toHaveBeenCalledWith('/api/memory/receipts/mrcpt_1/undo?workspace=personal')
  })

  it('offers no Undo when there is no change or it is not undoable', async () => {
    const view = await mountRows([
      historyRow({ id: 'a', via: 'agent' }),
      historyRow({ id: 'b', via: 'agent', change: { ...CHANGE, undoable: false } }),
      historyRow({ id: 'c', via: 'pwa', change: CHANGE }),
    ])
    expect(view.find('.ph-care-undo').exists()).toBe(false)
  })

  it('reports "Acted on prediction" rather than match/miss for an agent-settled row', async () => {
    const view = await mountRows([historyRow({
      via: 'agent',
      prediction: { verdict: 'accept', reason: 'durable', ts: 't', match: true },
    })])
    const badge = view.find('.ph-prediction .badge')
    expect(badge.text()).toBe('Acted on prediction')
    expect(badge.classes()).toContain('badge--muted')
  })
})
