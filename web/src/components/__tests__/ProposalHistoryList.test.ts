// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import ProposalHistoryList from '../ProposalHistoryList.vue'
import { useProposalsStore } from '../../stores/proposals'
import { useProjectStore } from '../../stores/projects'
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

describe('ProposalHistoryList', () => {
  let pinia: ReturnType<typeof createPinia>

  beforeEach(() => {
    pinia = createPinia()
    setActivePinia(pinia)
    apiGet.mockReset()
    apiPost.mockReset()
  })

  afterEach(() => {
    document.body.innerHTML = ''
    vi.restoreAllMocks()
  })

  it('fetches history on mount', async () => {
    apiGet.mockResolvedValue({ rows: [historyRow()], total: 1, truncated: false })
    const wrapper = mount(ProposalHistoryList, { global: { plugins: [pinia] } })
    await flushPromises()

    expect(apiGet).toHaveBeenCalledWith('/api/proposals/history?limit=200')
    expect(wrapper.text()).toContain('Remember the thing')
    wrapper.unmount()
  })

  it('shows an empty state with nothing loaded', async () => {
    apiGet.mockResolvedValue({ rows: [], total: 0, truncated: false })
    const wrapper = mount(ProposalHistoryList, { global: { plugins: [pinia] } })
    await flushPromises()

    expect(wrapper.find('.ph-empty').text()).toBe('No decisions yet.')
    wrapper.unmount()
  })

  it('groups rows by day and labels Today/Yesterday/older dates', async () => {
    const now = new Date()
    const yesterday = new Date(now)
    yesterday.setDate(now.getDate() - 1)
    const older = new Date(now)
    older.setDate(now.getDate() - 10)

    const store = useProposalsStore()
    store.historyRows = [
      historyRow({ id: 'today', ts: now.toISOString() }),
      historyRow({ id: 'yesterday', ts: yesterday.toISOString() }),
      historyRow({ id: 'older', ts: older.toISOString() }),
      historyRow({ id: 'legacy', ts: '' }),
    ]
    store.historyLoaded = true

    const wrapper = mount(ProposalHistoryList, { global: { plugins: [pinia] } })
    await flushPromises()

    const labels = wrapper.findAll('.ph-group-label').map(l => l.text())
    expect(labels[0]).toBe('Today')
    expect(labels[1]).toBe('Yesterday')
    expect(labels[labels.length - 1]).toBe('Earlier')
    wrapper.unmount()
  })

  it('labels an accepted duplicate as skipped rather than a fresh write', async () => {
    const store = useProposalsStore()
    store.historyRows = [
      historyRow({ id: 'dup', outcome: 'duplicate' }),
      historyRow({ id: 'suppressed', outcome: 'suppressed', via: 'auto' }),
      historyRow({ id: 'fresh', outcome: 'written' }),
    ]
    store.historyLoaded = true

    const wrapper = mount(ProposalHistoryList, { global: { plugins: [pinia] } })
    await flushPromises()

    const rows = wrapper.findAll('.ph-row')
    expect(rows[0]!.find('.badge').text()).toBe('Skipped · already known')
    expect(rows[1]!.find('.badge').text()).toBe('Skipped · already known')
    expect(rows[2]!.find('.badge').text()).toBe('Accepted')
    wrapper.unmount()
  })

  it('labels an expiry sweep dismissal distinctly from a plain dismiss', async () => {
    const store = useProposalsStore()
    store.historyRows = [
      historyRow({ id: 'swept', action: 'dismissed', outcome: 'swept' }),
      historyRow({ id: 'plain', action: 'dismissed', outcome: '' }),
    ]
    store.historyLoaded = true

    const wrapper = mount(ProposalHistoryList, { global: { plugins: [pinia] } })
    await flushPromises()

    const rows = wrapper.findAll('.ph-row')
    expect(rows[0]!.find('.badge').text()).toBe('Dismissed · expired')
    expect(rows[1]!.find('.badge').text()).toBe('Dismissed')
    wrapper.unmount()
  })

  it('filters by action and actor chips', async () => {
    const store = useProposalsStore()
    store.historyRows = [
      historyRow({ id: 'a', action: 'accepted', via: 'pwa' }),
      historyRow({ id: 'b', action: 'dismissed', via: 'agent' }),
      historyRow({ id: 'c', action: 'accepted', via: 'auto' }),
    ]
    store.historyLoaded = true

    const wrapper = mount(ProposalHistoryList, { global: { plugins: [pinia] } })
    await flushPromises()
    expect(wrapper.findAll('.ph-row')).toHaveLength(3)

    await wrapper.findAll('.ph-chip-row')[0]!.findAll('button')
      .find(b => b.text() === 'dismissed')!.trigger('click')
    expect(wrapper.findAll('.ph-row')).toHaveLength(1)

    await wrapper.findAll('.ph-chip-row')[0]!.findAll('button')
      .find(b => b.text() === 'all')!.trigger('click')
    await wrapper.findAll('.ph-chip-row')[1]!.findAll('button')
      .find(b => b.text() === 'automatic')!.trigger('click')
    expect(wrapper.findAll('.ph-row')).toHaveLength(1)
    expect(wrapper.text()).toContain('automatic')
    wrapper.unmount()
  })

  it('scopes to the active workspace, keeping install-wide rows', async () => {
    const store = useProposalsStore()
    const projects = useProjectStore()
    projects.activeWorkspace = 'work'
    store.historyRows = [
      historyRow({ id: 'personal-row', workspace: 'personal' }),
      historyRow({ id: 'work-row', workspace: 'work' }),
      historyRow({ id: 'global-row', workspace: '' }),
    ]
    store.historyLoaded = true

    const wrapper = mount(ProposalHistoryList, { global: { plugins: [pinia] } })
    await flushPromises()

    // personal-row is out of scope for the "work" workspace; the install-wide
    // row (no workspace) and the work row both stay.
    expect(wrapper.findAll('.ph-row')).toHaveLength(2)
    wrapper.unmount()
  })

  it('shows a "show more" button only when the server reports truncation', async () => {
    apiGet.mockResolvedValue({ rows: [historyRow()], total: 500, truncated: true })
    const wrapper = mount(ProposalHistoryList, { global: { plugins: [pinia] } })
    await flushPromises()

    const more = wrapper.find('.ph-more')
    expect(more.exists()).toBe(true)

    apiGet.mockResolvedValue({ rows: [historyRow(), historyRow({ id: 'h2' })], total: 500, truncated: false })
    await more.trigger('click')
    await flushPromises()

    expect(apiGet).toHaveBeenLastCalledWith('/api/proposals/history?limit=400')
    expect(wrapper.find('.ph-more').exists()).toBe(false)
    wrapper.unmount()
  })
})
