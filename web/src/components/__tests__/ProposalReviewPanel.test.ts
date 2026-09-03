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

describe('talk about it', () => {
  let pinia: ReturnType<typeof createPinia>

  beforeEach(() => {
    pinia = createPinia()
    setActivePinia(pinia)
    apiGet.mockReset()
    apiPost.mockReset()
  })

  it('offers a third action beside accept and dismiss, and leaves the row queued', async () => {
    // "Accept" writes the fact and "dismiss" drops it. Neither is right when the
    // operator does not yet know which — so a third action hands the row to a
    // chat in that row's workspace and changes nothing here.
    apiGet.mockResolvedValue({ rows: [row({ workspace: 'work' })] })
    const projects = useProjectStore()
    projects.activeWorkspace = 'work'
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    const labels = wrapper.findAll('.pr-actions button').map((b) => b.text())
    expect(labels).toEqual(['accept', 'dismiss', 'talk about it'])

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

  it('defaults to the Queue tab', async () => {
    mockProposalApi()
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    const tabs = wrapper.findAll('[role="tab"]')
    expect(tabs).toHaveLength(2)
    expect(tabs[0]!.text()).toBe('Queue')
    expect(tabs[1]!.text()).toContain('History')
    expect(tabs[0]!.attributes('aria-selected')).toBe('true')
    expect(wrapper.find('.pr-row').exists()).toBe(true)
    expect(apiGet).toHaveBeenCalledWith('/api/proposals')
    wrapper.unmount()
  })

  it('switching to History fetches and renders the decision ledger', async () => {
    mockProposalApi()
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    await wrapper.findAll('[role="tab"]')[1]!.trigger('click')
    await flushPromises()

    expect(apiGet).toHaveBeenCalledWith('/api/proposals/history?limit=200&workspace=personal')
    expect(wrapper.find('.pr-row').exists()).toBe(false)
    expect(wrapper.text()).toContain('Remember the thing')
    expect(wrapper.text()).toContain('Accepted')
    wrapper.unmount()
  })

  it('prefetches the ledger so the History count is there before the first open', async () => {
    // The count used to wait for the tab switch, so the badge appeared only
    // after the one moment it had something to tell you.
    mockProposalApi()
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    expect(apiGet).toHaveBeenCalledWith('/api/proposals/history?limit=200&workspace=personal')
    expect(wrapper.find('.pr-tab-count').text()).toBe('1')
    expect(wrapper.findAll('[role="tab"]')[0]!.attributes('aria-selected')).toBe('true')
    wrapper.unmount()
  })

  it('reports the scoped total in the badge, not the page size', async () => {
    // The page is capped, so a workspace with more decisions than the limit
    // showed the limit itself as though it were the whole ledger.
    apiGet.mockImplementation((path: string) => {
      if (path.startsWith('/api/proposals/history')) {
        return Promise.resolve({
          rows: [
            {
              id: 'h1', ts: '2026-09-01T10:00:00+00:00', action: 'accepted', via: 'pwa',
              kind: 'memory', text: 'Remember the thing', source: '', workspace: 'personal',
              destination: 'ciao:memory', outcome: 'written', proposal_id: 'p1',
            },
          ],
          total: 500,
          truncated: true,
          limit: 200,
          at_max: false,
        })
      }
      return Promise.resolve({ rows: [row({ id: 'a' })] })
    })
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    expect(wrapper.find('.pr-tab-count').text()).toBe('500')

    // Under a filter the visible count is the honest number.
    useProposalsStore().kindFilter = 'skill'
    await nextTick()
    expect(wrapper.find('.pr-tab-count').text()).toBe('0')
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

  it('renders no History count while the ledger is still unloaded', async () => {
    // Null, not zero: "History 0" on a ledger with hundreds of rows is the
    // opposite of what a badge is for.
    apiGet.mockImplementation((path: string) => {
      if (path.startsWith('/api/proposals/history')) return Promise.reject(new Error('nope'))
      return Promise.resolve({ rows: [row({ id: 'a' })] })
    })
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    expect(wrapper.find('.pr-tab-count').exists()).toBe(false)
    wrapper.unmount()
  })
})