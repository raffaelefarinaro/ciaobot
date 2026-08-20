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
    // The store re-fetches after the batch to reflect the server's list.
    expect(apiGet).toHaveBeenCalledTimes(2)
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
    // The PATH, because the whole content of the decision is in that file and
    // "a skill proposal file" told the operator nothing they could act on.
    expect(skillRow.text()).toContain('personal/Workspace/Skill-Proposals/proposal-2026-08-20.md')
    // Read it, build it, or drop it. `implement` is the primary because accepting
    // a proposed skill means implementing it — which is a chat, not a write.
    expect(skillRow.find('.btn-primary').text()).toBe('implement')
    expect(skillRow.findAll('.btn-chip').map(b => b.text()))
      .toEqual(['view', 'dismiss', 'talk about it'])
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

    const button = wrapper.findAll('.btn-chip').find(b => b.text() === 'view')!
    await button.trigger('click')
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
    vi.spyOn(projects, 'createChat').mockResolvedValue({ chat_id: 'chat-x' } as never)
    const send = vi.spyOn(projects, 'sendMessage').mockImplementation(() => undefined as never)
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
    // A proposal is a suggestion: implementing it must not silently resolve it.
    expect(act).not.toHaveBeenCalled()
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

  it('offers no accept for a re-home row nothing backs', async () => {
    // The old UI rendered "Move to a destination?" beside a confirm button that
    // could not name one.
    apiGet.mockResolvedValue({
      rows: [rehomeRow({ rehome: { note: 'personal/People/Mo.md', destination: '', candidates: [], justified: false, reason: 'no tag names a workspace' } })],
    })
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    const rowEl = wrapper.find('.pr-row')
    expect(rowEl.text()).toContain('needs a decision')
    expect(rowEl.find('.pr-actions .btn-primary').exists()).toBe(false)
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
})
