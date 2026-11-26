// @vitest-environment jsdom
//
// The History list says what each decision DID, not only what was decided: an
// approval whose write failed reads as not applied, undo is its own state, the
// decider is named, and an automated row links to its care run only when the
// run can be matched exactly. Rows from an older engine render without any of it.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { RouterLinkStub } from '@vue/test-utils'
import ProposalHistoryList from '../ProposalHistoryList.vue'
import type { ProposalHistoryRow } from '../../lib/types'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())
const routerPush = vi.hoisted(() => vi.fn())

vi.mock('../../lib/api', () => ({
  api: { get: apiGet, post: apiPost, patch: vi.fn(), del: vi.fn() },
}))
vi.mock('vue-router', () => ({ useRouter: () => ({ push: routerPush }) }))

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

async function mountRows(rows: ProposalHistoryRow[], schedules: unknown[] = [], careRun: unknown = null) {
  apiGet.mockImplementation(async (path: string) => {
    if (path.startsWith('/api/proposals/history')) return { rows, total: rows.length, truncated: false }
    if (path === '/api/schedules') return schedules
    if (path.endsWith('/care-run')) return { care_run: careRun }
    return {}
  })
  const wrapper = mount(ProposalHistoryList, {
    global: { plugins: [createPinia()], stubs: { RouterLink: RouterLinkStub } },
  })
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

describe('History apply state', () => {
  it('reads an applied approval as Applied', async () => {
    const view = await mountRows([row({ apply_status: 'applied', applied: true, actor: 'human' })])
    expect(view.find('.ph-apply').text()).toBe('Applied')
    expect(view.find('.badge').text()).toBe('Accepted')
  })

  it('reads an approval whose write failed as Not applied, never as Accepted', async () => {
    const view = await mountRows([row({ apply_status: 'failed', applied: false, actor: 'human' })])
    expect(view.find('.badge').text()).toBe('Not applied')
    expect(view.find('.ph-apply').text().startsWith('Not applied')).toBe(true)
    expect(view.find('.ph-apply').classes()).toContain('ph-apply--bad')
  })

  it('reads a conflict as Not applied', async () => {
    const view = await mountRows([row({ apply_status: 'conflict', applied: false })])
    expect(view.find('.ph-apply').text().startsWith('Not applied')).toBe(true)
  })

  it('shows Undone, and wins over the applied row it reverses', async () => {
    const view = await mountRows([row({ apply_status: 'undone', applied: false })])
    expect(view.find('.ph-apply').text()).toBe('Undone')
    expect(view.text()).not.toContain('Applied')
  })

  it('shows Rolled back and Pending', async () => {
    const view = await mountRows([
      row({ id: 'r', apply_status: 'rolled_back', applied: false }),
      row({ id: 'p', apply_status: 'pending', applied: null }),
    ])
    const lines = view.findAll('.ph-apply').map(l => l.text())
    expect(lines[0]).toBe('Rolled back')
    expect(lines[1].startsWith('Pending')).toBe(true)
  })

  it('reads an accepted row whose receipt is missing as Unverified, never Accepted', async () => {
    const view = await mountRows([
      row({ id: 'gone', apply_status: 'none', applied: null, receipt_issue: 'missing' }),
      row({ id: 'legacy', apply_status: 'none', applied: null }),
    ])
    const badges = view.findAll('.badge').map(b => b.text())
    expect(badges[0]).toBe('Unverified')
    expect(badges[1]).toBe('Accepted')
    const lines = view.findAll('.ph-apply')
    expect(lines).toHaveLength(1)
    expect(lines[0].text()).toContain('no longer in the journal')
    expect(lines[0].classes()).toContain('ph-apply--muted')
    expect(lines[0].classes()).not.toContain('ph-apply--bad')
  })

  it('shows no apply line for a dismissal with no receipt', async () => {
    const view = await mountRows([row({ action: 'dismissed', apply_status: 'none', applied: null })])
    expect(view.find('.ph-apply').exists()).toBe(false)
  })

})

describe('History decider and reason', () => {
  it('names a human decision You and an automated one Workspace care', async () => {
    const view = await mountRows([
      row({ id: 'a', actor: 'human', via: 'pwa' }),
      row({ id: 'b', actor: 'automated', via: 'auto', kind: 'skill' }),
    ])
    const chips = view.findAll('.ph-actor').map(c => c.text())
    expect(chips).toEqual(['You', 'Workspace care'])
  })

  it('shows the decided time as an absolute time, not a blank', async () => {
    const view = await mountRows([row({ decided_at: '2026-09-01T10:00:00+00:00' })])
    expect(view.find('.ph-time').text()).not.toBe('')
  })

  it('truncates a long reason and expands it on demand', async () => {
    const reason = 'because '.repeat(40).trim()
    const view = await mountRows([row({ reason })])
    const toggle = view.find('.ph-reason-toggle')
    expect(toggle.attributes('aria-expanded')).toBe('false')
    expect(view.find('.ph-reason').text().length).toBeLessThan(reason.length)
    await toggle.trigger('click')
    expect(toggle.attributes('aria-expanded')).toBe('true')
    expect(view.find('.ph-reason').text()).toContain(reason)
  })

  it('shows a short reason whole with no toggle', async () => {
    const view = await mountRows([row({ reason: 'short' })])
    expect(view.find('.ph-reason').text()).toContain('short')
    expect(view.find('.ph-reason-toggle').exists()).toBe(false)
  })
})

describe('History from an older engine', () => {
  it('renders without apply lines, NaN or undefined text', async () => {
    const view = await mountRows([row({ via: 'pwa' })])
    expect(view.find('.ph-apply').exists()).toBe(false)
    expect(view.find('.ph-actor').text()).toBe('You')
    expect(view.text()).not.toMatch(/NaN|undefined/)
  })
})

describe('History run link for automated rows', () => {
  const careSchedule = { schedule_id: 'system-memory-curation', last_run_chat_id: 'chat_a' }
  const careRun = { run_id: 'run_1', planned: 1, completed: 1, deferred: 0, failed: 0, counts_complete: true, reasons: [] }

  it('links to the schedule whose last run carries the row run id', async () => {
    const view = await mountRows(
      [row({ actor: 'automated', via: 'agent', kind: 'skill', run_id: 'run_1' })],
      [careSchedule],
      careRun,
    )
    const link = view.findComponent(RouterLinkStub)
    expect(link.exists()).toBe(true)
    expect(link.props('to')).toBe('/schedules/system-memory-curation')
  })

  it('shows no link when no care run matches the row run id', async () => {
    const view = await mountRows(
      [row({ actor: 'automated', via: 'agent', kind: 'skill', run_id: 'run_other' })],
      [careSchedule],
      careRun,
    )
    expect(view.findComponent(RouterLinkStub).exists()).toBe(false)
  })

  it('shows no link for a human row', async () => {
    const view = await mountRows([row({ actor: 'human' })], [careSchedule], careRun)
    expect(view.findComponent(RouterLinkStub).exists()).toBe(false)
  })
})
