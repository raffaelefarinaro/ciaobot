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

  it('does not mark a refused share publication as applied', async () => {
    const view = await mountRows([row({ kind: 'share', apply_status: 'failed', share_status: 'remote_rejected', applied: false })])
    expect(view.find('.badge').text()).toBe('Publication not confirmed')
    expect(view.find('.ph-apply').text()).toBe('Not published · remote_rejected')
    expect(view.text()).not.toContain('Applied')
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

  it('shows no apply line for a dismissal with no receipt', async () => {
    const view = await mountRows([row({ action: 'dismissed', apply_status: 'none', applied: null })])
    expect(view.find('.ph-apply').exists()).toBe(false)
  })

  it('shows the published commit and proposes a reversal through the review route', async () => {
    const share = row({
      kind: 'share', share_status: 'published', commit_oid: 'abc123', branch: 'main', path: 'docs/shared.md',
      change: { receipt_id: 'share-receipt', kind: 'share_publish', status: 'applied', destination: 'Team:docs/shared.md', undoable: true, changed: true, ts: '', commit_oid: 'abc123', branch: 'main' },
    })
    apiGet.mockImplementation(async (path: string) => {
      if (path.startsWith('/api/proposals/history')) return { rows: [share], total: 1, truncated: false }
      if (path.startsWith('/api/memory/receipts/share-receipt')) return {
        id: 'share-receipt', workspace: 'personal', kind: 'share_publish', status: 'applied', ts: '', actor: 'operator', source: 'pwa', destination: 'Team:docs/shared.md', fact_text: 'Shared note', undoable: true, has_snapshot: true, changed: true, share_status: 'published', commit_oid: 'abc123', before: 'old note\n', after: 'new note\n', diff: [{ op: 'removed', text: 'old note' }, { op: 'added', text: 'new note' }],
      }
      return {}
    })
    const reviewPath = '/memory/review?show=suggested&workspace=personal&proposal=prv_new'
    apiPost.mockResolvedValue({ review_path: reviewPath })

    const view = await mountRows([share])
    apiGet.mockImplementation(async (path: string) => path.startsWith('/api/memory/receipts/share-receipt') ? {
      id: 'share-receipt', workspace: 'personal', kind: 'share_publish', status: 'applied', ts: '', actor: 'operator', source: 'pwa', destination: 'Team:docs/shared.md', fact_text: 'Shared note', undoable: true, has_snapshot: true, changed: true, share_status: 'published', commit_oid: 'abc123', before: 'old note\n', after: 'new note\n', diff: [{ op: 'removed', text: 'old note' }, { op: 'added', text: 'new note' }],
    } : {})
    expect(view.find('.badge').text()).toBe('Published')
    expect(view.text()).toContain('abc123')
    await view.get('.ph-change-toggle').trigger('click')
    await flushPromises()
    expect(view.findAll('.ph-share-image pre').map(pre => pre.element.textContent)).toEqual(['old note\n', 'new note\n'])
    expect(view.find('.ph-change').text()).toContain('Published commit abc123')
    await view.get('.ph-change-actions button').trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith('/api/memory/receipts/share-receipt/undo?workspace=personal')
    expect(routerPush).toHaveBeenCalledWith(reviewPath)
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
