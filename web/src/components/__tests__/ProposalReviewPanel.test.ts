// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import ProposalReviewPanel from '../ProposalReviewPanel.vue'
import { useProposalsStore } from '../../stores/proposals'
import { useProjectStore } from '../../stores/projects'
import { useFileViewerStore } from '../../stores/fileViewer'
import type { ProposalRow } from '../../lib/types'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())

vi.mock('../../lib/api', () => ({
  api: { get: apiGet, post: apiPost, patch: vi.fn(), del: vi.fn() },
}))

function row(overrides: Partial<ProposalRow> = {}): ProposalRow {
  return {
    id: 'row-1',
    kind: 'memory',
    text: 'Remember the thing',
    source: '',
    workspace: 'personal',
    path: 'personal/Workspace/Memory-Proposals.md',
    line: 3,
    ...overrides,
  }
}

function rehomeRow(overrides: Partial<ProposalRow> = {}): ProposalRow {
  return row({
    id: 'rehome-1',
    kind: 'rehome',
    text: 'Move `personal/People/Mo.md` to work',
    rehome: {
      note: 'personal/People/Mo.md',
      destination: 'work',
      candidates: [],
      justified: false,
      reason: 'no live rehome signal for this note',
    },
    ...overrides,
  })
}

describe('ProposalReviewPanel', () => {
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

  it('renders rows from a mocked GET /api/proposals', async () => {
    apiGet.mockResolvedValue({ rows: [row({ id: 'a' }), row({ id: 'b', kind: 'profile' })] })
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    expect(apiGet).toHaveBeenCalledWith('/api/proposals')
    expect(wrapper.findAll('.pr-row')).toHaveLength(2)
    expect(wrapper.text()).toContain('Remember the thing')
    wrapper.unmount()
  })

  it('opens with one sentence and folds the mechanism into a disclosure', async () => {
    // The paragraph this replaces ran nine lines of internal routing, bounded
    // regions, stub notes and file removal BEFORE the first row — about 230px
    // of a 390px viewport. The lede has to be one sentence's worth of prose
    // and the rest has to start closed.
    apiGet.mockResolvedValue({ rows: [row({ id: 'a' })] })
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    const lede = wrapper.find('.pr-lede')
    expect(lede.exists()).toBe(true)
    expect(lede.text().length).toBeLessThan(200)

    const how = wrapper.find('.pr-how')
    expect(how.exists()).toBe(true)
    expect(how.attributes('open')).toBeUndefined()
    expect(wrapper.find('.pr-how-summary').text()).toBe('How memory works')
    wrapper.unmount()
  })

  it('keeps bounded regions and vault filenames out of the opening copy', async () => {
    apiGet.mockResolvedValue({ rows: [row({ id: 'a' })] })
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    const opening = `${wrapper.find('.pr-lede').text()} ${wrapper.find('.pr-how').text()}`
    for (const jargon of ['bounded', 'region', 'Workspace/Learnings.md', 'stub note', 'fallback queue']) {
      expect(opening, jargon).not.toContain(jargon)
    }
    wrapper.unmount()
  })

  it('says what keeping a row does, with the path one disclosure away', async () => {
    // `ciao:memory` is the same shape of string as `Workspace/Learnings.md`
    // and says nothing about the difference between them.
    apiGet.mockResolvedValue({ rows: [row({ id: 'a', kind: 'memory', region: 'memory' })] })
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    const sub = wrapper.find('.pr-row-sub')
    expect(sub.text()).toBe('Kept as a standing fact for this workspace')
    // Still reachable: the tooltip and the details line carry the path.
    expect(sub.attributes('title')).toBe('ciao:memory')
    expect(wrapper.find('.pr-row-detail').text()).toContain('Goes to ciao:memory')
    wrapper.unmount()
  })

  it('names every row checkbox by its kind and fact', async () => {
    // A bare checkbox is announced as "checkbox" with no clue which proposal it
    // selects; two rows were indistinguishable to a screen reader.
    apiGet.mockResolvedValue({
      rows: [
        row({ id: 'a', text: 'Remember the thing' }),
        row({ id: 'b', kind: 'profile', text: 'Prefers concise answers' }),
      ],
    })
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    const labels = wrapper.findAll('.pr-row-check').map(c => c.attributes('aria-label'))
    expect(labels).toEqual([
      'Select memory: Remember the thing',
      'Select profile: Prefers concise answers',
    ])
    // Unique, or the names do not distinguish the rows.
    expect(new Set(labels).size).toBe(labels.length)
    wrapper.unmount()
  })

  it('wraps the row checkbox in a 44px hit target', async () => {
    apiGet.mockResolvedValue({ rows: [row({ id: 'a' })] })
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    const hit = wrapper.find('.pr-row-check-hit')
    expect(hit.exists()).toBe(true)
    expect(hit.find('input[type="checkbox"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('renders a no-signal rehome row as a question, not a pre-filled accept', async () => {
    apiGet.mockResolvedValue({ rows: [rehomeRow()] })
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    const rowEl = wrapper.find('.pr-row')
    // The destination is presented as a question, not a one-click accept.
    expect(rowEl.text()).toContain('personal \u2192 work')
    // No single "accept" button that would pre-fill the destination.
    expect(rowEl.text()).not.toContain('accept')
    wrapper.unmount()
  })

  it('renders a dual-candidate rehome row as a picker with all candidates', async () => {
    apiGet.mockResolvedValue({
      rows: [rehomeRow({
        rehome: {
          note: 'personal/People/Mo.md',
          destination: 'work',
          candidates: ['work', 'client'],
          justified: false,
          reason: 'dual tag',
        },
      })],
    })
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    const rowEl = wrapper.find('.pr-row')
    // The candidates are the primary buttons: one per workspace the tags name.
    // Never a pre-filled single accept, because no one candidate is backed.
    const candidates = rowEl.findAll('.pr-actions .btn-primary').map(o => o.text())
    expect(candidates).toEqual(['work', 'client'])
    // And the non-committal options stay available.
    const chips = rowEl.findAll('.pr-actions .btn-chip').map(o => o.text())
    expect(chips).toEqual(['dismiss', 'talk about it'])
    wrapper.unmount()
  })

  it('shows a leak warning before an accept is confirmed', async () => {
    apiGet.mockResolvedValue({
      rows: [row({ id: 'leak', kind: 'memory', workspace: 'work', leak_warning: true, region: 'memory' })],
    })
    useProjectStore().activeWorkspace = 'work'
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    const rowEl = wrapper.find('.pr-row')
    expect(rowEl.text()).toContain('visible in every workspace')

    // Clicking accept opens the confirm step, not the API call.
    await rowEl.find('.btn-primary').trigger('click')
    await nextTick()
    expect(apiPost).not.toHaveBeenCalled()
    expect(rowEl.text()).toContain('Sure?')

    // Confirming sends the accept.
    await rowEl.find('.pr-actions--confirm .btn-primary').trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith('/api/proposals/leak/accept')
    wrapper.unmount()
  })

  it('batch accept calls the batch endpoint with selected ids and re-renders from its response', async () => {
    apiGet.mockResolvedValue({
      rows: [row({ id: 'a' }), row({ id: 'b' }), row({ id: 'c' })],
    })
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    // Select all, then accept.
    await wrapper.find('.pr-group-select input').setValue(true)
    await nextTick()
    await wrapper.find('.pr-batch .btn-primary').trigger('click')
    await flushPromises()

    expect(apiPost).toHaveBeenCalledWith('/api/proposals/batch', {
      action: 'accept',
      ids: ['a', 'b', 'c'],
    })
    // The store re-fetches the queue after the batch to reflect the server's
    // list. Counted per endpoint: the panel also prefetches and refreshes the
    // decision ledger for the History tab's badge.
    const queueCalls = apiGet.mock.calls.filter(([path]) => path === '/api/proposals')
    expect(queueCalls).toHaveLength(2)
    wrapper.unmount()
  })

  it('batch dismiss calls the batch endpoint with selected ids', async () => {
    apiGet.mockResolvedValue({ rows: [row({ id: 'a' }), row({ id: 'b' })] })
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    await wrapper.find('.pr-group-select input').setValue(true)
    await nextTick()
    await wrapper.find('.pr-batch .btn-chip').trigger('click')
    await flushPromises()

    expect(apiPost).toHaveBeenCalledWith('/api/proposals/batch', {
      action: 'dismiss',
      ids: ['a', 'b'],
    })
    wrapper.unmount()
  })

  it('dismiss-older-than calls its endpoint with a resolved date', async () => {
    apiGet.mockResolvedValue({ rows: [row({ id: 'a' })] })
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    // Default is 30 days; set a deterministic value.
    const input = wrapper.find('.pr-older-input')
    await input.setValue(7)
    await nextTick()
    await wrapper.find('.pr-foot .btn-chip').trigger('click')
    await flushPromises()

    const expected = new Date()
    expected.setDate(expected.getDate() - 7)
    const iso = expected.toISOString().slice(0, 10)
    expect(apiPost).toHaveBeenCalledWith(`/api/proposals/dismiss-older-than?date=${iso}`)
    wrapper.unmount()
  })

  it('gives a skill row the actions a FILE has, never a region edit', async () => {
    apiGet.mockResolvedValue({
      rows: [
        row({ id: 'bullet', kind: 'memory' }),
        row({ id: 'skill', kind: 'skill', text: 'proposal-2026-08-20', path: 'personal/Workspace/Skill-Proposals/proposal-2026-08-20.md', line: -1 }),
      ],
    })
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    const skillRow = wrapper.findAll('.pr-row').find(r => r.text().includes('proposal-2026-08-20'))!
    expect(skillRow.find('.pr-kind').text()).toBe('skill')
    // The path IS the button — a separate "view" spent an action slot saying what
    // the path already said. Only the leaf: every row in a group shares the folder.
    const link = skillRow.find('.pr-path-link')
    expect(link.text()).toBe('proposal-2026-08-20.md')
    expect(link.attributes('title')).toBe('personal/Workspace/Skill-Proposals/proposal-2026-08-20.md')
    // Build it or drop it. `implement` is the primary because accepting a
    // proposed skill means implementing it — which is a chat, not a write.
    expect(skillRow.find('.btn-primary').text()).toBe('implement')
    expect(skillRow.findAll('.btn-chip').map(b => b.text()))
      .toEqual(['dismiss', 'talk about it'])
    wrapper.unmount()
  })

  it('view opens the proposal file itself', async () => {
    const path = 'personal/Workspace/Skill-Proposals/proposal-2026-08-20.md'
    apiGet.mockResolvedValue({
      rows: [row({ id: 'skill', kind: 'skill', text: 'proposal-2026-08-20', path, line: -1 })],
    })
    const viewer = useFileViewerStore()
    const open = vi.spyOn(viewer, 'open').mockResolvedValue(true)
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    await wrapper.find('.pr-path-link').trigger('click')
    await flushPromises()

    expect(open).toHaveBeenCalledWith(path)
    wrapper.unmount()
  })

  it('implement opens a chat in the row own workspace and leaves the row queued', async () => {
    const path = 'work/Workspace/Skill-Proposals/proposal-2026-08-20.md'
    apiGet.mockResolvedValue({
      rows: [row({
        id: 'skill', kind: 'skill', text: 'proposal-2026-08-20',
        workspace: 'work', path, line: -1,
      })],
    })
    const projects = useProjectStore()
    projects.activeWorkspace = 'work'
    projects.projects = [{
      project_id: 'p-work', name: 'General', workspace: 'work', context: '',
      created_at: '', order: 0, vault_folder: 'general', is_auto: true,
    } as never]
    apiPost.mockResolvedValue({ chat_id: 'chat-x', project_id: 'p-work' } as never)
    const send = vi.spyOn(projects, 'sendMessage').mockImplementation(() => true as never)
    const proposals = useProposalsStore()
    const act = vi.spyOn(proposals, 'act')
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    const button = wrapper.findAll('.btn-primary').find(b => b.text() === 'implement')!
    await button.trigger('click')
    await flushPromises()

    const sent = String(send.mock.calls.at(-1)?.[1] ?? '')
    expect(sent).toContain(path)
    expect(sent).toContain('skills/')
    expect(sent).toContain('do not delegate')
    expect(apiPost).toHaveBeenCalledWith('/api/projects/p-work/chats', {
      title: 'Implement proposal-2026-08-20',
      helper: {
        kind: 'proposal',
        intent: 'resolve',
        proposal_ids: ['skill'],
        archive_policy: 'when_resolved',
      },
    })
    // A proposal is a suggestion: implementing it must not silently resolve it.
    expect(act).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('groups the same skill proposed on several dates', async () => {
    // New reflections upsert one canonical file. Keep grouping old dated rows
    // until every existing workspace has converged through another pass.
    apiGet.mockResolvedValue({
      rows: [
        row({ id: 's1', kind: 'skill', text: '2026-08-09-defuddle', path: 'p/Workspace/Skill-Proposals/2026-08-09-defuddle.md', line: -1 }),
        row({ id: 's2', kind: 'skill', text: '2026-08-16-defuddle', path: 'p/Workspace/Skill-Proposals/2026-08-16-defuddle.md', line: -1 }),
        row({ id: 's3', kind: 'skill', text: '2026-08-12-jira-tickets', path: 'p/Workspace/Skill-Proposals/2026-08-12-jira-tickets.md', line: -1 }),
      ],
    })
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    const labels = wrapper.findAll('.pr-group-label-name').map(l => l.text())
    expect(labels).toEqual(['defuddle', 'jira-tickets'])
    const counts = wrapper.findAll('.pr-group-label-count').map(c => c.text())
    expect(counts).toEqual(['2', '1'])
    // Newest first inside a group.
    const firstGroupRows = wrapper.findAll('.pr-rows')[0].findAll('.pr-row')
    expect(firstGroupRows[0].text()).toContain('2026-08-16')
    wrapper.unmount()
  })

  it('does not group memory or re-home rows', async () => {
    // One fact about one note: grouping those would invent a relationship.
    apiGet.mockResolvedValue({
      rows: [row({ id: 'a', kind: 'memory' }), row({ id: 'b', kind: 'memory' })],
    })
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    expect(wrapper.findAll('.pr-group-label')).toHaveLength(0)
    expect(wrapper.findAll('.pr-row')).toHaveLength(2)
    wrapper.unmount()
  })

  it('the picker sends the workspace it was asked for', async () => {
    // Every candidate button used to call accept with no destination, so the
    // answer to "which workspace?" was thrown away and nothing could move.
    apiGet.mockResolvedValue({
      rows: [row({
        id: 'r', kind: 'rehome',
        rehome: {
          note: 'personal/People/Oliver.md', destination: '', reason: '',
          candidates: ['personal', 'work'], justified: false,
        },
      })],
    })
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    const workButton = wrapper.findAll('.btn-primary').find(b => b.text() === 'work')!
    await workButton.trigger('click')
    await flushPromises()

    expect(apiPost).toHaveBeenCalledWith('/api/proposals/r/accept?workspace=work')
    wrapper.unmount()
  })

  it('a justified re-home says where the note is going', async () => {
    apiGet.mockResolvedValue({
      rows: [row({
        id: 'j', kind: 'rehome',
        rehome: {
          note: 'personal/People/Mo.md', destination: 'work/People/Mo.md',
          reason: '', candidates: ['work'], justified: true,
        },
      })],
    })
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    expect(wrapper.find('.btn-primary').text()).toBe('move to work')
    wrapper.unmount()
  })

  it('excludes skill rows from batch accept', async () => {
    apiGet.mockResolvedValue({
      rows: [
        row({ id: 'bullet', kind: 'memory' }),
        row({ id: 'skill', kind: 'skill', text: 'proposal-2026-08-20', line: -1 }),
      ],
    })
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    await wrapper.find('.pr-group-select input').setValue(true)
    await nextTick()
    // The accept button counts only non-skill rows.
    expect(wrapper.find('.pr-batch .btn-primary').text()).toContain('1')
    await wrapper.find('.pr-batch .btn-primary').trigger('click')
    await flushPromises()

    expect(apiPost).toHaveBeenCalledWith('/api/proposals/batch', {
      action: 'accept',
      ids: ['bullet'],
    })
    wrapper.unmount()
  })
})

describe('queue load states', () => {
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

  it('never shows a zero-state success message while the first GET is delayed', async () => {
    // The bug: a slow initial fetch fell through to "Nothing queued here." and
    // read as a successfully cleared queue before the server had answered.
    let release!: (value: { rows: ProposalRow[] }) => void
    const pendingQueue = new Promise<{ rows: ProposalRow[] }>((r) => { release = r })
    apiGet.mockImplementation((path: string) => {
      if (String(path).startsWith('/api/proposals/history')) {
        return Promise.resolve({ rows: [], total: 0, truncated: false })
      }
      return pendingQueue
    })

    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await nextTick()

    expect(wrapper.text()).toContain('Loading proposals…')
    expect(wrapper.text()).not.toContain('Nothing queued here.')
    expect(wrapper.text()).not.toContain('All reviewed.')

    // The delayed response lands: the zero-state is replaced by the real queue.
    release({ rows: [row({ id: 'a' })] })
    await flushPromises()
    expect(wrapper.find('.pr-row').exists()).toBe(true)
    wrapper.unmount()
  })

  it('reports a rejected first GET with Retry and no empty-queue claim', async () => {
    apiGet.mockRejectedValue(new Error('proposals are unreachable'))
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    expect(wrapper.find('.pr-error-block').exists()).toBe(true)
    expect(wrapper.text()).toContain('proposals are unreachable')
    expect(wrapper.find('.pr-error-block').text()).toContain('retry')
    expect(wrapper.text()).not.toContain('Nothing queued here.')
    expect(wrapper.text()).not.toContain('All reviewed.')
    // No rows section at all while the list could not be read.
    expect(wrapper.find('.pr-group').exists()).toBe(false)

    // Retry issues a fresh request and renders the rows it returns.
    apiGet.mockResolvedValue({ rows: [row({ id: 'a' })] })
    await wrapper.find('.pr-error-block .btn-chip').trigger('click')
    await flushPromises()

    expect(wrapper.find('.pr-row').exists()).toBe(true)
    expect(wrapper.find('.pr-error-block').exists()).toBe(false)
    wrapper.unmount()
  })

  it('keeps existing rows after a failed refresh and labels them stale', async () => {
    apiGet.mockResolvedValueOnce({ rows: [row({ id: 'a' }), row({ id: 'b' })] })
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()
    expect(wrapper.findAll('.pr-row')).toHaveLength(2)

    // A forced refresh (as after a mutation) fails. Rows must stay.
    apiGet.mockRejectedValueOnce(new Error('refresh broke'))
    await useProposalsStore().fetch({ force: true })
    await nextTick()

    expect(wrapper.findAll('.pr-row')).toHaveLength(2)
    expect(wrapper.find('.pr-stale').exists()).toBe(true)
    expect(wrapper.find('.pr-stale').text()).toContain('last loaded queue')
    expect(wrapper.text()).not.toContain('Nothing queued here.')
    expect(wrapper.text()).not.toContain('All reviewed.')
    expect(wrapper.find('.pr-error-block').exists()).toBe(false)

    // Retry succeeds: the stale notice clears.
    apiGet.mockResolvedValueOnce({ rows: [row({ id: 'a' })] })
    await wrapper.find('.pr-stale .btn-chip').trigger('click')
    await flushPromises()

    expect(wrapper.find('.pr-stale').exists()).toBe(false)
    expect(wrapper.findAll('.pr-row')).toHaveLength(1)
    wrapper.unmount()
  })

  it('offers Clear filters when a filter hides the whole queue', async () => {
    apiGet.mockResolvedValue({ rows: [row({ id: 'a', kind: 'memory' })] })
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    useProposalsStore().search = 'nothing matches this'
    await nextTick()

    expect(wrapper.text()).toContain('No proposals match the current filters')
    expect(wrapper.text()).not.toContain('All reviewed.')
    await wrapper.find('.pr-clear-filter').trigger('click')
    await flushPromises()

    expect(wrapper.findAll('.pr-row')).toHaveLength(1)
    wrapper.unmount()
  })

  it('says "All reviewed." only after a successful load of an empty queue', async () => {
    apiGet.mockResolvedValue({ rows: [] })
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    expect(wrapper.text()).toContain('All reviewed.')
    expect(wrapper.text()).not.toContain('Loading proposals…')
    expect(wrapper.text()).not.toContain('Nothing queued here.')
    wrapper.unmount()
  })
})

describe('talk about it', () => {
  let pinia: ReturnType<typeof createPinia>

  beforeEach(() => {
    pinia = createPinia()
    setActivePinia(pinia)
    apiGet.mockReset()
    apiPost.mockReset()
  })

  it('offers a talk-about-it action beside the decisions, and leaves the row queued', async () => {
    // "Accept" writes the fact and "dismiss" drops it. Neither is right when the
    // operator does not yet know which — so a further action hands the row to a
    // chat in that row's workspace and changes nothing here.
    apiGet.mockResolvedValue({ rows: [row({ workspace: 'work' })] })
    const projects = useProjectStore()
    projects.activeWorkspace = 'work'
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    const labels = wrapper.findAll('.pr-actions button').map((b) => b.text())
    expect(labels).toEqual(['accept', 'check first', 'dismiss', 'talk about it'])

    const store = useProposalsStore()
    const act = vi.spyOn(store, 'act')
    await wrapper
      .findAll('.pr-actions button')
      .find((b) => b.text() === 'talk about it')!
      .trigger('click')
    // It is not a decision: nothing resolves the row.
    expect(act).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})

describe('workspace scoping', () => {
  let pinia: ReturnType<typeof createPinia>

  beforeEach(() => {
    pinia = createPinia()
    setActivePinia(pinia)
    apiGet.mockReset()
    apiPost.mockReset()
  })

  it('shows only the active workspace, plus install-wide rows', async () => {
    // The workspace switcher on the left is where every other page keeps this
    // choice. Grouping in the list put it in a heading you had to scroll back to.
    apiGet.mockResolvedValue({
      rows: [
        row({ id: 'p', workspace: 'personal', text: 'personal fact' }),
        row({ id: 'w', workspace: 'work', text: 'work fact' }),
        row({ id: 's', workspace: '', text: 'install-wide fact' }),
      ],
    })
    const projects = useProjectStore()
    projects.activeWorkspace = 'work'
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    const text = wrapper.text()
    expect(text).toContain('work fact')
    expect(text).toContain('install-wide fact')
    expect(text).not.toContain('personal fact')
    wrapper.unmount()
  })

  it('shows everything when no workspace is active yet', async () => {
    // Hiding every row until a switcher reports a selection reads as an empty
    // queue on a single-workspace install.
    apiGet.mockResolvedValue({
      rows: [row({ id: 'p', workspace: 'personal', text: 'a fact' })],
    })
    const projects = useProjectStore()
    projects.activeWorkspace = ''
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    expect(wrapper.text()).toContain('a fact')
    wrapper.unmount()
  })

  it('offers a destination picker for a re-home row nothing backs', async () => {
    // Never a PRE-FILLED accept — the old UI rendered "Move to a destination?"
    // beside a confirm button that could not name one. But it must offer the
    // choice: accepting now performs a real move, and most queued rows have no
    // tag naming anywhere, so offering them nothing left all fourteen unmovable.
    apiGet.mockResolvedValue({
      rows: [rehomeRow({ rehome: { note: 'personal/People/Mo.md', destination: '', candidates: [], justified: false, reason: 'no tag names a workspace' } })],
    })
    const projects = useProjectStore()
    projects.workspaces = [
      { name: 'personal', vault_root: '/p', default_provider: 'claude', gws_profile: '' },
      { name: 'work', vault_root: '/w', default_provider: 'claude', gws_profile: '' },
    ] as never
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    const rowEl = wrapper.find('.pr-row')
    expect(rowEl.text()).toContain('Move to')
    // Its own workspace is not a destination, and nothing is pre-selected.
    expect(rowEl.findAll('.pr-actions .btn-primary').map(b => b.text())).toEqual(['work'])
    wrapper.unmount()
  })

  it('never offers a batch accept for rows that have no accept', async () => {
    // The bug: the batch bar said "accept 1" for a re-home row whose own actions
    // correctly showed none, and accepting one drops the bullet while moving
    // nothing — so a batch could silently discard proposals the UI had just said
    // it could not act on.
    apiGet.mockResolvedValue({
      rows: [rehomeRow({ id: 'r', rehome: { note: 'personal/People/Mo.md', destination: '', candidates: [], justified: false, reason: 'none' } })],
    })
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    await wrapper.find('.pr-row-check').setValue(true)

    const batch = wrapper.find('.pr-batch')
    expect(batch.text()).toContain('1 selected')
    const labels = batch.findAll('button').map((b) => b.text())
    expect(labels).not.toContain('accept 1')
    expect(labels.some((l) => l.startsWith('accept'))).toBe(false)
    // Dismiss and discuss remain available for the selection.
    expect(labels).toContain('dismiss 1')
    expect(labels).toContain('talk about 1')
    wrapper.unmount()
  })

  it('discusses a whole selection in one chat', async () => {
    apiGet.mockResolvedValue({
      rows: [row({ id: 'a' }), row({ id: 'b', kind: 'profile' })],
    })
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    await wrapper.find('.pr-group-select input').setValue(true)
    const store = useProposalsStore()
    const act = vi.spyOn(store, 'act')
    const batch = vi.spyOn(store, 'batch')
    await wrapper.find('.pr-batch').findAll('button')
      .find((b) => b.text() === 'talk about 2')!.trigger('click')

    // Talking is not deciding: nothing resolves.
    expect(act).not.toHaveBeenCalled()
    expect(batch).not.toHaveBeenCalled()
    wrapper.unmount()
  })
  it('a batch only acts on rows the current filters are showing', async () => {
    // The bug: `selected` lives in the store and survives a filter change,
    // while the list filters client-side, so selecting everything and then
    // narrowing the kind chip left the batch bar acting on rows that were no
    // longer on screen — dismiss discarded proposals the user could not see.
    apiGet.mockResolvedValue({
      rows: [
        row({ id: 'm', kind: 'memory' }),
        row({ id: 's', kind: 'skill', text: 'proposal-2026-08-20', path: 'personal/Workspace/Skill-Proposals/proposal-2026-08-20.md', line: -1 }),
      ],
    })
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    await wrapper.find('.pr-group-select input').setValue(true)
    await nextTick()
    expect(wrapper.find('.pr-batch').text()).toContain('2 selected')

    // Narrow to one kind: the skill row is selected but no longer visible.
    useProposalsStore().kindFilter = 'memory'
    await nextTick()

    const batch = wrapper.find('.pr-batch')
    expect(batch.text()).toContain('1 selected')
    await batch.findAll('button').find(b => b.text() === 'dismiss 1')!.trigger('click')
    await flushPromises()

    expect(apiPost).toHaveBeenCalledWith('/api/proposals/batch', {
      action: 'dismiss',
      ids: ['m'],
    })
    wrapper.unmount()
  })

  it('a workspace switch leaves the other workspace out of the batch', async () => {
    // Select-all in `work`, switch the sidebar to `personal`, press dismiss —
    // the work rows must not be discarded, and the bar must not count them.
    apiGet.mockResolvedValue({
      rows: [
        row({ id: 'w1', workspace: 'work' }),
        row({ id: 'w2', workspace: 'work' }),
        row({ id: 'p1', workspace: 'personal' }),
      ],
    })
    const projects = useProjectStore()
    projects.activeWorkspace = 'work'
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    await wrapper.find('.pr-group-select input').setValue(true)
    await nextTick()
    expect(wrapper.find('.pr-batch').text()).toContain('2 selected')

    projects.activeWorkspace = 'personal'
    await nextTick()
    // Nothing visible is selected, so there is no batch to press at all.
    expect(wrapper.find('.pr-batch').exists()).toBe(false)

    // Selecting the row that IS on screen dismisses only that one.
    await wrapper.find('.pr-row-check').setValue(true)
    await nextTick()
    await wrapper.find('.pr-batch').findAll('button')
      .find(b => b.text() === 'dismiss 1')!.trigger('click')
    await flushPromises()

    expect(apiPost).toHaveBeenCalledWith('/api/proposals/batch', {
      action: 'dismiss',
      ids: ['p1'],
    })
    wrapper.unmount()
  })

  it('a batch accept skips selected rows the filters hide', async () => {
    apiGet.mockResolvedValue({
      rows: [
        row({ id: 'w1', workspace: 'work' }),
        row({ id: 'p1', workspace: 'personal' }),
      ],
    })
    const projects = useProjectStore()
    projects.activeWorkspace = 'work'
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    await wrapper.find('.pr-group-select input').setValue(true)
    await nextTick()
    projects.activeWorkspace = 'personal'
    await nextTick()
    await wrapper.find('.pr-row-check').setValue(true)
    await nextTick()

    expect(wrapper.find('.pr-batch .btn-primary').text()).toBe('accept 1')
    await wrapper.find('.pr-batch .btn-primary').trigger('click')
    await flushPromises()

    expect(apiPost).toHaveBeenCalledWith('/api/proposals/batch', {
      action: 'accept',
      ids: ['p1'],
    })
    wrapper.unmount()
  })
})


describe('Queue / History tabs', () => {
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

  function mockProposalApi() {
    apiGet.mockImplementation((url: string) => {
      if (url.startsWith('/api/proposals/history')) {
        return Promise.resolve({
          rows: [
            {
              id: 'h1', ts: '2026-09-01T10:00:00+00:00', action: 'accepted', via: 'pwa',
              kind: 'memory', text: 'Remember the thing', source: '', workspace: 'personal',
              destination: 'ciao:memory', outcome: 'written', proposal_id: 'p1',
            },
          ],
          total: 1,
          truncated: false,
        })
      }
      return Promise.resolve({ rows: [row({ id: 'a' })] })
    })
  }

  it('defaults to the queue and carries no tab bar of its own', async () => {
    // The Queue/History tablist moved up to the Review surface, which now
    // renders one bar for all four of its sections instead of three stacked
    // rows of tabs. A bar left behind here would render directly under it.
    mockProposalApi()
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    expect(wrapper.findAll('[role="tab"]')).toHaveLength(0)
    expect(wrapper.find('.pr-row').exists()).toBe(true)
    expect(apiGet).toHaveBeenCalledWith('/api/proposals')
    wrapper.unmount()
  })

  it('renders the decision ledger when the parent selects the history section', async () => {
    mockProposalApi()
    const wrapper = mount(ProposalReviewPanel, {
      props: { section: 'history' as const },
      global: { plugins: [pinia] },
    })
    await flushPromises()

    expect(apiGet).toHaveBeenCalledWith('/api/proposals/history?limit=200&workspace=personal')
    expect(wrapper.find('.pr-row').exists()).toBe(false)
    expect(wrapper.text()).toContain('Remember the thing')
    expect(wrapper.text()).toContain('Accepted')
    wrapper.unmount()
  })

  it('switches section when the parent changes the prop, without remounting', async () => {
    mockProposalApi()
    const wrapper = mount(ProposalReviewPanel, {
      props: { section: 'queue' as const },
      global: { plugins: [pinia] },
    })
    await flushPromises()
    expect(wrapper.find('.pr-row').exists()).toBe(true)

    await wrapper.setProps({ section: 'history' as const })
    await flushPromises()

    expect(wrapper.find('.pr-row').exists()).toBe(false)
    expect(wrapper.text()).toContain('Remember the thing')
    expect(useProposalsStore().view).toBe('history')
    wrapper.unmount()
  })

  it('refreshes history after a direct per-row accept', async () => {
    // The primary accept button posts directly and calls `store.fetch`, which
    // deliberately leaves history alone. With the ledger prefetched on mount,
    // switching to History reused the cached page and the badge stayed stale.
    mockProposalApi()
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    const historyCalls = () =>
      apiGet.mock.calls.filter(([p]) => String(p).startsWith('/api/proposals/history')).length
    const before = historyCalls()

    apiPost.mockResolvedValue({} as never)
    await wrapper.find('.pr-row .btn-primary').trigger('click')
    await flushPromises()

    expect(apiPost).toHaveBeenCalledWith(expect.stringContaining('/accept'))
    expect(historyCalls()).toBeGreaterThan(before)
    wrapper.unmount()
  })

})

// The History badge itself is now rendered by the Review surface's single tab
// bar, so its counting rules are pinned in `MemoryMapView.test.ts`.
describe('reconcile before writing', () => {
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

  /** A 409 the way `lib/api` throws one: the message for people, the body for us. */
  function refusal(payload: Record<string, unknown>): Error {
    const err = new Error(String(payload.error ?? 'refused'))
    Object.assign(err, { status: 409, payload })
    return err
  }

  const deferral = {
    error: 'reconciling against ciao:memory could not decide (reconcile unavailable), '
      + 'so nothing was written and this stays queued.',
    id: 'a',
    region: 'memory',
    deferred: true,
    reason: 'reconcile unavailable for ciao:memory',
    competing: ['Office is in Zurich. [2026-01-01]'],
  }

  function clickLabel(wrapper: ReturnType<typeof mount>, label: string) {
    return wrapper.findAll('.pr-actions button').find((b) => b.text() === label)!.trigger('click')
  }

  it('asks the server to reconcile only when the check is requested', async () => {
    // The plain accept is one synchronous write; reconciling is a model call,
    // so the query parameter has to be absent unless it was asked for.
    apiGet.mockResolvedValue({ rows: [row({ id: 'a', kind: 'memory' })] })
    apiPost.mockResolvedValue({} as never)
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    await clickLabel(wrapper, 'accept')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith('/api/proposals/a/accept')

    apiPost.mockClear()
    await clickLabel(wrapper, 'check first')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith('/api/proposals/a/accept?reconcile=1')
    wrapper.unmount()
  })

  it('offers no check on a kind that has no entries to be weighed against', async () => {
    // Only the bounded regions hold entries a new fact can supersede. A person
    // note or the learnings list would take the parameter and ignore it.
    apiGet.mockResolvedValue({ rows: [row({ id: 'a', kind: 'learnings' })] })
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    const labels = wrapper.findAll('.pr-actions button').map((b) => b.text())
    expect(labels).toEqual(['accept', 'dismiss', 'talk about it'])
    wrapper.unmount()
  })

  it('shows what a deferred accept was weighed against, and retries from there', async () => {
    // The whole point of deferring rather than appending: the fact may replace
    // an entry the region already holds. A refusal that does not say which one
    // leaves nothing to decide with.
    apiGet.mockResolvedValue({ rows: [row({ id: 'a', kind: 'memory' })] })
    apiPost.mockRejectedValueOnce(refusal(deferral))
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    await clickLabel(wrapper, 'check first')
    await flushPromises()

    const box = wrapper.find('.pr-actions--deferred')
    expect(box.exists()).toBe(true)
    expect(box.text()).toContain('reconcile unavailable for ciao:memory')
    expect(box.text()).toContain('Office is in Zurich. [2026-01-01]')
    // It replaces the decisions rather than sitting beside them: the next step
    // is about these entries, not accept-or-dismiss.
    expect(wrapper.findAll('.pr-actions button').map((b) => b.text()))
      .toEqual(['try again', 'leave it queued'])
    // A deferral is not one of the refusals that hand the row to a merge chat —
    // it has a cheaper remedy right here.
    expect(apiPost).toHaveBeenCalledTimes(1)
    // And the row is still queued, because nothing was written.
    expect(wrapper.findAll('.pr-row')).toHaveLength(1)

    // Retrying reconciles again, against the region as it stands now.
    apiPost.mockResolvedValue({} as never)
    await wrapper.find('.pr-actions--deferred .btn-primary').trigger('click')
    await flushPromises()

    expect(apiPost).toHaveBeenLastCalledWith('/api/proposals/a/accept?reconcile=1')
    expect(wrapper.find('.pr-actions--deferred').exists()).toBe(false)
    wrapper.unmount()
  })

  it('keeps the notice when a retry defers again, and clears it on dismissal', async () => {
    apiGet.mockResolvedValue({ rows: [row({ id: 'a', kind: 'memory' })] })
    apiPost.mockRejectedValue(refusal(deferral))
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    await clickLabel(wrapper, 'check first')
    await flushPromises()
    await wrapper.find('.pr-actions--deferred .btn-primary').trigger('click')
    await flushPromises()

    expect(apiPost).toHaveBeenCalledTimes(2)
    expect(wrapper.find('.pr-actions--deferred').exists()).toBe(true)

    // Dismissing the notice is not a decision about the row: it goes back to
    // accept/dismiss with the fact still queued.
    await wrapper.find('.pr-actions--deferred .btn-chip').trigger('click')
    await nextTick()
    expect(wrapper.find('.pr-actions--deferred').exists()).toBe(false)
    expect(wrapper.findAll('.pr-actions button').map((b) => b.text()))
      .toEqual(['accept', 'check first', 'dismiss', 'talk about it'])
    wrapper.unmount()
  })

  it('confirms a leak warning before a requested reconcile, and keeps it', async () => {
    // The check still writes the region, so the row that warns about writing a
    // guide every workspace loads must warn about this one too — and the
    // confirm must not silently drop the reconcile it was asked for.
    apiGet.mockResolvedValue({
      rows: [row({ id: 'a', kind: 'memory', leak_warning: true, region: 'memory' })],
    })
    apiPost.mockResolvedValue({} as never)
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    await clickLabel(wrapper, 'check first')
    await nextTick()
    expect(apiPost).not.toHaveBeenCalled()
    expect(wrapper.find('.pr-actions--confirm').exists()).toBe(true)

    await wrapper.find('.pr-actions--confirm .btn-primary').trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith('/api/proposals/a/accept?reconcile=1')
    wrapper.unmount()
  })
})
