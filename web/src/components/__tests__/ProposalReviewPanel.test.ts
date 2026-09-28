// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import ProposalReviewPanel from '../ProposalReviewPanel.vue'
import { useProposalsStore } from '../../stores/proposals'
import { useProjectStore } from '../../stores/projects'
import { useFileViewerStore } from '../../stores/fileViewer'
import type { ProposalPreview, ProposalRow } from '../../lib/types'

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

/** A server preview: what accepting a row would write, and the revision it was
 * computed against. */
function preview(overrides: Partial<ProposalPreview> = {}): ProposalPreview {
  return {
    id: 'row-1',
    workspace: 'personal',
    kind: 'memory',
    text: 'Remember the thing',
    source: '',
    action: 'edit_region',
    operation: 'add',
    destination: 'ciao:memory',
    destination_path: '/w/AGENTS.md',
    revision: 'rev-1',
    before: '- An older fact.',
    after: '- An older fact.\n- Remember the thing [2026-09-19]',
    exact: true,
    truncated: false,
    can_accept: true,
    reason: '',
    separator: '\n',
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

/** Row checkboxes appear only after "Select" (or once something is selected). */
async function enterSelectMode(wrapper: { find: (s: string) => { exists: () => boolean; text: () => string; trigger: (e: string) => Promise<void> } }) {
  const toggle = wrapper.find('.pr-select-toggle')
  if (toggle.exists() && toggle.text() === 'Select') await toggle.trigger('click')
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

  it('renders backtick spans in a fact as inline code, never as raw HTML', async () => {
    apiGet.mockResolvedValue({ rows: [row({ id: 'a', text: 'Apps get `<name>.scandbox.io` domains via `get_any_app`.' })] })
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    const codes = wrapper.findAll('.pr-row-title .pr-inline-code').map(c => c.text())
    expect(codes).toEqual(['<name>.scandbox.io', 'get_any_app'])
    // The angle brackets stay text: nothing model-written becomes markup.
    expect(wrapper.find('.pr-row-title name').exists()).toBe(false)
    expect(wrapper.get('.pr-row-title').text()).not.toContain('`')
    wrapper.unmount()
  })

  it('opens with a heading and one sentence, and no how-it-works disclosure', async () => {
    apiGet.mockResolvedValue({ rows: [row({ id: 'a' })] })
    useProjectStore().activeWorkspace = 'personal'
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    expect(wrapper.get('h2').text()).toBe('1 suggested')
    const lede = wrapper.get('.pr-lede').text()
    expect(lede.length).toBeLessThan(200)
    expect(lede).toContain('create a note, add to one, or change one')
    expect(wrapper.find('.pr-how').exists()).toBe(false)
    wrapper.unmount()
  })

  it('keeps bounded regions and vault filenames out of the opening copy', async () => {
    apiGet.mockResolvedValue({ rows: [row({ id: 'a' })] })
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    const opening = wrapper.find('.pr-lede').text()
    for (const jargon of ['bounded', 'region', 'Workspace/Learnings.md', 'stub note', 'fallback queue']) {
      expect(opening, jargon).not.toContain(jargon)
    }
    wrapper.unmount()
  })

  it('says what keeping a row does while its preview is on the way', async () => {
    // `ciao:memory` is the same shape of string as `Workspace/Learnings.md`
    // and says nothing about the difference between them.
    apiGet.mockImplementation((url: string) => (url.includes('/preview')
      ? new Promise(() => {})
      : Promise.resolve({ rows: [row({ id: 'a', kind: 'memory', region: 'memory' })] })))
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    const sub = wrapper.find('.pr-row-sub')
    expect(sub.text()).toContain('Kept as a standing fact for this workspace')
    expect(sub.text()).toContain('checking what it changes')
    // Still reachable: the tooltip carries the path.
    expect(sub.attributes('title')).toBe('ciao:memory')
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

    await enterSelectMode(wrapper)
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

    await enterSelectMode(wrapper)
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
    expect(rowEl.find('.pr-op').text()).toBe('Needs a decision')
    expect(rowEl.text()).toContain('Moves this note to the work workspace')
    expect(rowEl.find('.pr-change').attributes('title')).toContain('personal \u2192 work')
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
    const candidates = rowEl.findAll('.pr-actions .pr-row-action').map(o => o.text())
    expect(candidates).toEqual(['work', 'client'])
    // And the non-committal options stay available.
    const chips = rowEl.findAll('.pr-actions button:not(.pr-row-action)').map(o => o.text())
    expect(chips).toEqual(['Dismiss', 'Discuss'])
    wrapper.unmount()
  })

  it('carries the leak warning into the decision card, which confirms the accept', async () => {
    // The warning used to open a confirmation of its own. It now rides on the
    // card, which is where the destination and the replacement already are —
    // two stacked confirmations for one decision is one too many.
    apiGet.mockImplementation((url: string) => {
      if (url.startsWith('/api/proposals/leak/preview')) {
        return Promise.resolve({
          ok: true,
          preview: preview({ id: 'leak', destination: 'ciao:memory', leak_warning: true }),
        })
      }
      return Promise.resolve({
        rows: [row({ id: 'leak', kind: 'memory', workspace: 'work', leak_warning: true, region: 'memory' })],
      })
    })
    useProjectStore().activeWorkspace = 'work'
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    const rowEl = wrapper.find('.pr-row')
    expect(rowEl.text()).toContain('visible in every workspace')

    // "Edit first" opens the card, not the API call, and the warning rides on it.
    await rowEl.find('.pr-edit-first').trigger('click')
    await flushPromises()
    expect(apiPost).not.toHaveBeenCalled()
    expect(wrapper.find('.pr-card').text()).toContain('visible in every workspace')

    // Confirming sends the accept, pinned to the revision the card showed.
    await wrapper.find('.pr-actions--card .btn-primary').trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith('/api/proposals/leak/accept', {
      expected_revision: 'rev-1',
    })
    wrapper.unmount()
  })

  it('batch accept calls the batch endpoint with selected ids and re-renders from its response', async () => {
    apiGet.mockResolvedValue({
      rows: [row({ id: 'a' }), row({ id: 'b' }), row({ id: 'c' })],
    })
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    // Select all, then accept.
    await enterSelectMode(wrapper)
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

    await enterSelectMode(wrapper)
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

    // The clean-up lives in the section menu and asks for the day count.
    const prompt = await import('../../lib/prompt')
    await wrapper.find('.pr-more').trigger('keydown', { key: 'Enter' })
    await nextTick()
    const item = wrapper.find('.pr-dismiss-older')
    expect(item.exists()).toBe(true)
    await item.trigger('click')
    await flushPromises()
    expect(prompt.pendingPrompt.value?.value).toBe('30')
    prompt.pendingPrompt.value!.resolve('7')
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
    expect(skillRow.find('.pr-row-action').text()).toBe('Implement')
    expect(skillRow.findAll('.pr-actions button:not(.pr-row-action)').map(b => b.text()))
      .toEqual(['Dismiss', 'Discuss'])
    expect(skillRow.find('.pr-op').text()).toBe('New skill')
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

    const button = wrapper.findAll('.pr-row-action').find(b => b.text() === 'Implement')!
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

    const workButton = wrapper.findAll('.pr-row-action').find(b => b.text() === 'work')!
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

    expect(wrapper.find('.pr-row-action').text()).toBe('Move to work')
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

    await enterSelectMode(wrapper)
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
    expect(wrapper.find('.pr-error-block').text()).toContain('Retry')
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

  it('offers a third action beside review and dismiss, and leaves the row queued', async () => {
    // "Review" opens the decision card and "dismiss" drops the row. Neither is
    // right when the operator does not yet know which — so a third action hands
    // the row to a chat in that row's workspace and changes nothing here.
    apiGet.mockResolvedValue({ rows: [row({ workspace: 'work' })] })
    const projects = useProjectStore()
    projects.activeWorkspace = 'work'
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    const labels = wrapper.findAll('.pr-actions button').map((b) => b.text())
    expect(labels).toEqual(['Review', 'Dismiss', 'Edit first', 'Discuss'])

    const store = useProposalsStore()
    const act = vi.spyOn(store, 'act')
    await wrapper
      .findAll('.pr-actions button')
      .find((b) => b.text() === 'Discuss')!
      .trigger('click')
    // It is not a decision: nothing resolves the row.
    expect(act).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('swaps talk about it for a link back to the open chat, keeping the decision buttons', async () => {
    // The toast is the only other way back into the chat, and it fades. The row
    // still needs a decision, so review and dismiss stay where they were.
    localStorage.removeItem('ciao:proposal-discuss-links')
    apiGet.mockResolvedValue({ rows: [row({ workspace: 'work' })] })
    const projects = useProjectStore()
    projects.activeWorkspace = 'work'
    projects.projects = [{
      project_id: 'p-work', name: 'General', workspace: 'work', context: '',
      created_at: '', order: 0, vault_folder: 'general', is_auto: true,
    } as never]
    apiPost.mockResolvedValue({ chat_id: 'chat-d', project_id: 'p-work', archived: false } as never)
    vi.spyOn(projects, 'sendMessage').mockImplementation(() => true as never)
    const switchChat = vi.spyOn(projects, 'switchChat').mockResolvedValue(undefined as never)
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    await wrapper
      .findAll('.pr-actions button')
      .find((b) => b.text() === 'Discuss')!
      .trigger('click')
    await flushPromises()

    const labels = wrapper.findAll('.pr-actions button').map((b) => b.text())
    expect(labels).toEqual(['Review', 'Dismiss', 'Edit first', 'Open chat'])
    await wrapper
      .findAll('.pr-actions button')
      .find((b) => b.text() === 'Open chat')!
      .trigger('click')
    await flushPromises()
    expect(switchChat).toHaveBeenCalledWith('chat-d')

    // A reload mounts before the chat list arrives; the link must survive that.
    wrapper.unmount()
    const chats = projects.chats
    projects.chats = []
    const reloaded = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()
    projects.chats = chats
    await flushPromises()
    expect(reloaded.findAll('.pr-actions button').map((b) => b.text()))
      .toEqual(['Review', 'Dismiss', 'Edit first', 'Open chat'])
    await testArchiveDropsLink(reloaded)
  })

  async function testArchiveDropsLink(wrapper: ReturnType<typeof mount>) {
    const projects = useProjectStore()
    // Archiving the chat drops the link and brings the action back.
    projects.chats.find((c) => c.chat_id === 'chat-d')!.archived = true
    await flushPromises()
    expect(wrapper.findAll('.pr-actions button').map((b) => b.text()))
      .toEqual(['Review', 'Dismiss', 'Edit first', 'Discuss'])
    wrapper.unmount()
  }
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
    expect(rowEl.findAll('.pr-actions .pr-row-action').map(b => b.text())).toEqual(['work'])
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

    await enterSelectMode(wrapper)
    await wrapper.find('.pr-row-check').setValue(true)

    const batch = wrapper.find('.pr-batch')
    expect(batch.text()).toContain('1 selected')
    const labels = batch.findAll('button').map((b) => b.text())
    expect(labels).not.toContain('Accept 1')
    expect(labels.some((l) => l.startsWith('accept'))).toBe(false)
    // Dismiss and discuss remain available for the selection.
    expect(labels).toContain('Dismiss 1')
    expect(labels).toContain('Talk about 1')
    wrapper.unmount()
  })

  it('discusses a whole selection in one chat', async () => {
    apiGet.mockResolvedValue({
      rows: [row({ id: 'a' }), row({ id: 'b', kind: 'profile' })],
    })
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    await enterSelectMode(wrapper)
    await wrapper.find('.pr-group-select input').setValue(true)
    const store = useProposalsStore()
    const act = vi.spyOn(store, 'act')
    const batch = vi.spyOn(store, 'batch')
    await wrapper.find('.pr-batch').findAll('button')
      .find((b) => b.text() === 'Talk about 2')!.trigger('click')

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

    await enterSelectMode(wrapper)
    await wrapper.find('.pr-group-select input').setValue(true)
    await nextTick()
    expect(wrapper.find('.pr-batch').text()).toContain('2 selected')

    // Narrow to one kind: the skill row is selected but no longer visible.
    useProposalsStore().kindFilter = 'memory'
    await nextTick()

    const batch = wrapper.find('.pr-batch')
    expect(batch.text()).toContain('1 selected')
    await batch.findAll('button').find(b => b.text() === 'Dismiss 1')!.trigger('click')
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

    await enterSelectMode(wrapper)
    await wrapper.find('.pr-group-select input').setValue(true)
    await nextTick()
    expect(wrapper.find('.pr-batch').text()).toContain('2 selected')

    projects.activeWorkspace = 'personal'
    await nextTick()
    // Nothing visible is selected, so there is no batch to press at all.
    expect(wrapper.find('.pr-batch').exists()).toBe(false)

    // Selecting the row that IS on screen dismisses only that one.
    await enterSelectMode(wrapper)
    await wrapper.find('.pr-row-check').setValue(true)
    await nextTick()
    await wrapper.find('.pr-batch').findAll('button')
      .find(b => b.text() === 'Dismiss 1')!.trigger('click')
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

    await enterSelectMode(wrapper)
    await wrapper.find('.pr-group-select input').setValue(true)
    await nextTick()
    projects.activeWorkspace = 'personal'
    await nextTick()
    await enterSelectMode(wrapper)
    await wrapper.find('.pr-row-check').setValue(true)
    await nextTick()

    expect(wrapper.find('.pr-batch .btn-primary').text()).toBe('Accept 1')
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
      if (url.startsWith('/api/proposals/a/preview')) {
        return Promise.resolve({ ok: true, preview: preview({ id: 'a' }) })
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
    // The preview is on the row, so the row's own button accepts it.
    await wrapper.find('.pr-row .pr-row-accept').trigger('click')
    await flushPromises()

    expect(apiPost).toHaveBeenCalledWith('/api/proposals/a/accept', { expected_revision: 'rev-1' })
    expect(historyCalls()).toBeGreaterThan(before)
    wrapper.unmount()
  })

})

// The History badge itself is now rendered by the Review surface's single tab
// bar, so its counting rules are pinned in `MemoryMapView.test.ts`.
describe('decision card', () => {
  let pinia: ReturnType<typeof createPinia>

  beforeEach(() => {
    pinia = createPinia()
    setActivePinia(pinia)
    apiGet.mockReset()
    apiPost.mockReset()
    useProjectStore().activeWorkspace = 'personal'
  })

  afterEach(() => {
    document.body.innerHTML = ''
    vi.restoreAllMocks()
  })

  /** Queue + preview, with the preview overridable per test. */
  function mockQueue(previewOverrides: Partial<ProposalPreview> = {}) {
    apiGet.mockImplementation((url: string) => {
      if (url.startsWith('/api/proposals/history')) {
        return Promise.resolve({ rows: [], total: 0, truncated: false })
      }
      if (url.startsWith('/api/proposals/row-1/preview')) {
        return Promise.resolve({ ok: true, preview: preview(previewOverrides) })
      }
      return Promise.resolve({ rows: [row()] })
    })
  }

  /** "Edit first" opens the card on its editor; cancelling the edit leaves
   * the card showing the change and its actions. */
  async function openCard() {
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()
    await wrapper.find('.pr-row .pr-edit-first').trigger('click')
    await flushPromises()
    const cancel = wrapper.findAll('.pr-card button').find(b => b.text() === 'Cancel edit')
    if (cancel) await cancel.trigger('click')
    await flushPromises()
    return wrapper
  }

  it('shows the destination, operation, source and exact replacement before accepting', async () => {
    // The bullet's own text is NOT the change: the promotion reconciles against
    // whatever the destination holds now and stamps a learned-at date. The card
    // is what closes that gap, so it has to carry all four.
    apiGet.mockImplementation((url: string) => {
      if (url.startsWith('/api/proposals/history')) {
        return Promise.resolve({ rows: [], total: 0, truncated: false })
      }
      if (url.startsWith('/api/proposals/row-1/preview')) {
        return Promise.resolve({ ok: true, preview: preview() })
      }
      return Promise.resolve({ rows: [row({ source: 'chat-42' })] })
    })
    const wrapper = await openCard()

    const card = wrapper.find('.pr-card')
    expect(card.exists()).toBe(true)
    expect(card.find('.pr-card-op').text()).toBe('Add')
    expect(card.find('.pr-card-dest').text()).toBe('ciao:memory')
    expect(card.text()).toContain('chat-42')
    // The replacement the SERVER computed, stamp and all - not the row's text.
    const added = card.findAll('.pr-card-diff-line--added').map(l => l.text())
    expect(added.join(' ')).toContain('Remember the thing [2026-09-19]')
    // Nothing has been written yet.
    expect(apiPost).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('renders an Update as a replacement, not a second copy', async () => {
    // The other branch of the operation badge. Every card exercised so far has
    // been an Add, because a region accept always appends; an Update is what a
    // `[learnings]` bullet produces when the destination already holds that
    // learning and accepting bumps its recurrence count instead of filing it
    // twice. That is exactly the case the bullet's own text cannot describe,
    // so the card has to show the line it replaces beside the line it writes.
    const filed =
      '- [thing] [2026-09-01 → 2026-09-01] (x1) Remember the thing. — sources: chat-1'
    const bumped =
      '- [thing] [2026-09-01 → 2026-09-19] (x2) Remember the thing. — sources: chat-1, chat-42'
    apiGet.mockImplementation((url: string) => {
      if (url.startsWith('/api/proposals/history')) {
        return Promise.resolve({ rows: [], total: 0, truncated: false })
      }
      if (url.startsWith('/api/proposals/row-1/preview')) {
        return Promise.resolve({
          ok: true,
          preview: preview({
            kind: 'learnings',
            action: 'append_learnings',
            operation: 'update',
            destination: 'Workspace/Learnings.md',
            destination_path: '/v/Workspace/Learnings.md',
            before: `## Active\n\n${filed}\n`,
            after: `## Active\n\n${bumped}\n`,
            reason: 'this learning is already filed; accepting bumps its recurrence count',
          }),
        })
      }
      return Promise.resolve({ rows: [row({ kind: 'learnings', source: 'chat-42' })] })
    })
    const wrapper = await openCard()

    const card = wrapper.find('.pr-card')
    expect(card.find('.pr-card-op').text()).toBe('Update')
    expect(card.find('.pr-card-op').classes()).toContain('pr-card-op--update')
    expect(card.find('.pr-card-dest').text()).toBe('Workspace/Learnings.md')
    // One line out, one line in — the untouched heading is not reported.
    expect(card.findAll('.pr-card-diff-line--removed').map(l => l.text())).toEqual([
      expect.stringContaining(filed),
    ])
    expect(card.findAll('.pr-card-diff-line--added').map(l => l.text())).toEqual([
      expect.stringContaining(bumped),
    ])
    expect(card.text()).toContain('accepting bumps its recurrence count')
    expect(card.find('.pr-actions--card .btn-primary').text()).toBe(
      'Save to Workspace/Learnings.md',
    )
    // A learnings row is not reconcilable, so the check-first chip stays away.
    expect(wrapper.findAll('.pr-actions--card button').map(b => b.text())).not.toContain(
      'Check first',
    )
    expect(apiPost).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('offers one primary action with edit, discuss and dismiss beside it', async () => {
    mockQueue()
    const wrapper = await openCard()

    const actions = wrapper.findAll('.pr-actions--card button').map(b => b.text())
    expect(actions[0]).toBe('Save to ciao:memory')
    // "check first" is the same write with a reconcile in front of it, so it is
    // a secondary beside edit/discuss/dismiss rather than a second primary.
    expect(actions.slice(1)).toEqual([
      'Check first', 'Edit suggestion', 'Discuss', 'Dismiss', 'Cancel',
    ])
    expect(wrapper.findAll('.pr-actions--card .btn-primary')).toHaveLength(1)
    wrapper.unmount()
  })

  it('sends the previewed revision with the accept', async () => {
    mockQueue()
    apiPost.mockResolvedValue({} as never)
    const wrapper = await openCard()

    await wrapper.find('.pr-actions--card .btn-primary').trigger('click')
    await flushPromises()

    expect(apiPost).toHaveBeenCalledWith('/api/proposals/row-1/accept', {
      expected_revision: 'rev-1',
    })
    wrapper.unmount()
  })

  it('re-renders the card from the conflict response instead of writing', async () => {
    // The destination moved while the card was on screen. The server refuses
    // and hands back a refreshed preview; showing THAT is the whole point, so
    // the operator decides against the destination as it is now.
    mockQueue()
    const wrapper = await openCard()
    const conflict = Object.assign(new Error('the destination changed'), {
      payload: {
        conflict: true,
        preview: preview({
          revision: 'rev-2',
          before: '- An older fact.\n- Someone else wrote this.',
          after: '- An older fact.\n- Someone else wrote this.\n- Remember the thing [2026-09-19]',
        }),
      },
    })
    apiPost.mockRejectedValue(conflict)

    await wrapper.find('.pr-actions--card .btn-primary').trigger('click')
    await flushPromises()

    const card = wrapper.find('.pr-card')
    expect(card.exists()).toBe(true)
    expect(card.text()).toContain('The destination changed since this preview')
    // The diff is recomputed against the body that is there NOW: the fact
    // someone else wrote is context, not a change this accept would make.
    expect(card.text()).not.toContain('Someone else wrote this')
    expect(useProposalsStore().previews['row-1'].revision).toBe('rev-2')
    wrapper.unmount()
  })

  it('previews an edited wording against the same destination', async () => {
    mockQueue()
    const wrapper = await openCard()

    await wrapper.findAll('.pr-actions--card button').find(b => b.text() === 'Edit suggestion')!.trigger('click')
    await nextTick()
    await wrapper.find('.pr-card-edit-input').setValue('Remember the other thing')
    await wrapper.find('.pr-card-edit .btn-primary').trigger('click')
    await flushPromises()

    expect(apiGet).toHaveBeenCalledWith(
      '/api/proposals/row-1/preview?text=Remember%20the%20other%20thing',
    )
    // An edited accept used to be warned about here: the ledger records the
    // ORIGINAL bullet, so History could not match the decision to the write.
    // The decision now carries the receipt's id, so the change and its undo
    // are there like any other and the caveat would be false.
    expect(wrapper.find('.pr-card').text()).not.toContain('cannot be undone')
    wrapper.unmount()
  })

  it('offers no accept for a preview the server says cannot be written', async () => {
    mockQueue({ can_accept: false, operation: 'none', after: '- An older fact.', reason: 'this reads as an event' })
    const wrapper = await openCard()

    expect(wrapper.find('.pr-actions--card .btn-primary').exists()).toBe(false)
    expect(wrapper.find('.pr-card').text()).toContain('this reads as an event')
    wrapper.unmount()
  })

  it('says so rather than guessing when the replacement cannot be known', async () => {
    mockQueue({ exact: false, before: 'doc', after: '', operation: 'update', destination: 'Projects/Ciao.md' })
    const wrapper = await openCard()

    expect(wrapper.find('.pr-card').text()).toContain('decided when you accept')
    wrapper.unmount()
  })

  it('summarises a bulk accept per destination and keeps the failures', async () => {
    apiGet.mockImplementation((url: string) => {
      if (url.startsWith('/api/proposals/history')) {
        return Promise.resolve({ rows: [], total: 0, truncated: false })
      }
      return Promise.resolve({ rows: [row({ id: 'a' }), row({ id: 'b' })] })
    })
    apiPost.mockResolvedValue({
      ok: true,
      action: 'accept',
      results: [
        { id: 'a', action: 'edit_region', dismissed: true, promoted: true },
        { id: 'b', action: 'edit_region', dismissed: false, promoted: false, conflict: true, error: 'changed' },
      ],
      summary: [
        {
          destination: 'ciao:memory', action: 'accept', total: 2, ok: 1, failed: 1,
          conflicts: 1, duplicates: 0, failed_ids: ['b'], errors: ['changed'],
        },
      ],
    } as never)
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    await enterSelectMode(wrapper)
    await wrapper.find('.pr-group-select input').setValue(true)
    await nextTick()
    await wrapper.find('.pr-batch .btn-primary').trigger('click')
    await flushPromises()

    const summary = wrapper.find('.pr-summary-block')
    expect(summary.exists()).toBe(true)
    expect(summary.text()).toContain('ciao:memory')
    expect(summary.text()).toContain('1 of 2 applied')
    expect(summary.text()).toContain('1 changed underneath')
    expect(summary.text()).toContain('1 still queued')
    wrapper.unmount()
  })
})

describe('reconcile before writing', () => {
  let pinia: ReturnType<typeof createPinia>

  beforeEach(() => {
    pinia = createPinia()
    setActivePinia(pinia)
    apiGet.mockReset()
    apiPost.mockReset()
    useProjectStore().activeWorkspace = 'personal'
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
    id: 'row-1',
    region: 'memory',
    deferred: true,
    reason: 'reconcile unavailable for ciao:memory',
    competing: ['Office is in Zurich. [2026-01-01]'],
  }

  /** Queue + preview for one row, so the decision card can be opened on it. */
  function mockQueue(rowOverrides: Partial<ProposalRow> = {}, previewOverrides: Partial<ProposalPreview> = {}) {
    apiGet.mockImplementation((url: string) => {
      if (url.startsWith('/api/proposals/history')) {
        return Promise.resolve({ rows: [], total: 0, truncated: false })
      }
      if (url.startsWith('/api/proposals/row-1/preview')) {
        return Promise.resolve({ ok: true, preview: preview(previewOverrides) })
      }
      return Promise.resolve({ rows: [row(rowOverrides)] })
    })
  }

  /** The check is offered on the card, not on the row: it is the same write as
   * the primary, so it belongs where the change being written is on screen. */
  /** "Edit first" opens the card on its editor; cancelling the edit leaves
   * the card showing the change and its actions. */
  async function openCard() {
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()
    await wrapper.find('.pr-row .pr-edit-first').trigger('click')
    await flushPromises()
    const cancel = wrapper.findAll('.pr-card button').find(b => b.text() === 'Cancel edit')
    if (cancel) await cancel.trigger('click')
    await flushPromises()
    return wrapper
  }

  function cardLabels(wrapper: ReturnType<typeof mount>) {
    return wrapper.findAll('.pr-actions--card button').map((b) => b.text())
  }

  function clickCard(wrapper: ReturnType<typeof mount>, label: string) {
    return wrapper.findAll('.pr-actions--card button').find((b) => b.text() === label)!.trigger('click')
  }

  it('asks the server to reconcile only when the check is requested', async () => {
    // The plain save is one synchronous write; reconciling is a model call, so
    // the query parameter has to be absent unless it was asked for.
    mockQueue()
    apiPost.mockResolvedValue({} as never)
    const wrapper = await openCard()

    await clickCard(wrapper, 'Save to ciao:memory')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith('/api/proposals/row-1/accept', {
      expected_revision: 'rev-1',
    })

    apiPost.mockClear()
    await wrapper.find('.pr-row .pr-row-action').trigger('click')
    await flushPromises()
    await clickCard(wrapper, 'Check first')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith('/api/proposals/row-1/accept?reconcile=1', {
      expected_revision: 'rev-1',
    })
    wrapper.unmount()
  })

  it('offers no check on a kind that has no entries to be weighed against', async () => {
    // Only the bounded regions hold entries a new fact can supersede. A person
    // note or the learnings list would take the parameter and ignore it.
    mockQueue({ kind: 'learnings' }, { kind: 'learnings', destination: 'Workspace/Learnings.md' })
    const wrapper = await openCard()

    expect(cardLabels(wrapper)).toEqual([
      'Save to Workspace/Learnings.md', 'Edit suggestion', 'Discuss', 'Dismiss', 'Cancel',
    ])
    wrapper.unmount()
  })

  it('offers the check beside the primary on a region row', async () => {
    mockQueue()
    const wrapper = await openCard()

    expect(cardLabels(wrapper)).toEqual([
      'Save to ciao:memory', 'Check first', 'Edit suggestion', 'Discuss', 'Dismiss', 'Cancel',
    ])
    // One primary: the check is the same write, not a competing decision.
    expect(wrapper.findAll('.pr-actions--card .btn-primary')).toHaveLength(1)
    wrapper.unmount()
  })

  it('shows what a deferred accept was weighed against, and retries from there', async () => {
    // The whole point of deferring rather than appending: the fact may replace
    // an entry the region already holds. A refusal that does not say which one
    // leaves nothing to decide with.
    mockQueue()
    apiPost.mockRejectedValueOnce(refusal(deferral))
    const wrapper = await openCard()

    await clickCard(wrapper, 'Check first')
    await flushPromises()

    const box = wrapper.find('.pr-actions--deferred')
    expect(box.exists()).toBe(true)
    expect(box.text()).toContain('reconcile unavailable for ciao:memory')
    expect(box.text()).toContain('Office is in Zurich. [2026-01-01]')
    // It replaces the decisions rather than sitting beside them: the next step
    // is about these entries, not save-or-dismiss. The card closes with it.
    expect(wrapper.find('.pr-card').exists()).toBe(false)
    expect(wrapper.findAll('.pr-actions button').map((b) => b.text()))
      .toEqual(['Try again', 'Leave it queued'])
    // A deferral is not one of the refusals that hand the row to a merge chat —
    // it has a cheaper remedy right here.
    expect(apiPost).toHaveBeenCalledTimes(1)
    // Nor is it a toast: the reason belongs on the row it describes.
    expect(useProposalsStore().error).toBe('')
    // And the row is still queued, because nothing was written.
    expect(wrapper.findAll('.pr-row')).toHaveLength(1)

    // Retrying reconciles again, against the region as it stands now — pinned
    // to the same revision the card showed, so the retry cannot land on a
    // destination nobody looked at.
    apiPost.mockResolvedValue({} as never)
    await wrapper.find('.pr-actions--deferred .mr-btn:not(.mr-btn--quiet)').trigger('click')
    await flushPromises()

    expect(apiPost).toHaveBeenLastCalledWith('/api/proposals/row-1/accept?reconcile=1', {
      expected_revision: 'rev-1',
    })
    expect(wrapper.find('.pr-actions--deferred').exists()).toBe(false)
    wrapper.unmount()
  })

  it('a deferred edited accept retries with the approved wording, not the original bullet', async () => {
    // The operator edited the wording, checked first, and the check deferred.
    // The retry must resend the approved text with its revision: resending
    // the original bullet would silently replace what was just approved, and
    // resending no revision would leave the retry unguarded.
    mockQueue({}, { text: 'Remember the other thing' })
    apiPost.mockRejectedValueOnce(refusal(deferral))
    const wrapper = await openCard()

    await wrapper.findAll('.pr-actions--card button').find(b => b.text() === 'Edit suggestion')!.trigger('click')
    await nextTick()
    await wrapper.find('.pr-card-edit-input').setValue('Remember the other thing')
    await wrapper.find('.pr-card-edit .btn-primary').trigger('click')
    await flushPromises()
    await clickCard(wrapper, 'Check first')
    await flushPromises()

    expect(wrapper.find('.pr-actions--deferred').exists()).toBe(true)

    apiPost.mockResolvedValue({} as never)
    await wrapper.find('.pr-actions--deferred .mr-btn:not(.mr-btn--quiet)').trigger('click')
    await flushPromises()

    expect(apiPost).toHaveBeenLastCalledWith('/api/proposals/row-1/accept?reconcile=1', {
      expected_revision: 'rev-1',
      text: 'Remember the other thing',
    })
    wrapper.unmount()
  })

  it('keeps the notice when a retry defers again, and clears it on dismissal', async () => {
    mockQueue()
    apiPost.mockRejectedValue(refusal(deferral))
    const wrapper = await openCard()

    await clickCard(wrapper, 'Check first')
    await flushPromises()
    await wrapper.find('.pr-actions--deferred .mr-btn:not(.mr-btn--quiet)').trigger('click')
    await flushPromises()

    expect(apiPost).toHaveBeenCalledTimes(2)
    expect(wrapper.find('.pr-actions--deferred').exists()).toBe(true)

    // Dismissing the notice is not a decision about the row: it goes back to
    // review/dismiss with the fact still queued.
    await wrapper.find('.pr-actions--deferred .mr-btn--quiet').trigger('click')
    await nextTick()
    expect(wrapper.find('.pr-actions--deferred').exists()).toBe(false)
    expect(wrapper.findAll('.pr-actions button').map((b) => b.text()))
      .toEqual(['Add entry', 'Dismiss', 'Edit first', 'Discuss'])
    wrapper.unmount()
  })

  it('carries the leak warning into the card the check is confirmed from', async () => {
    // The check still writes the region, so the row that warns about writing a
    // guide every workspace loads must warn about this one too. The card is
    // that confirmation — clicking the check on it IS the consent, which is why
    // nothing is posted until then.
    mockQueue(
      { leak_warning: true, region: 'memory' },
      { leak_warning: true },
    )
    apiPost.mockResolvedValue({} as never)
    const wrapper = await openCard()

    expect(apiPost).not.toHaveBeenCalled()
    expect(wrapper.find('.pr-card').text()).toContain('visible in every workspace')

    await clickCard(wrapper, 'Check first')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith('/api/proposals/row-1/accept?reconcile=1', {
      expected_revision: 'rev-1',
    })
    wrapper.unmount()
  })
})

describe('the change on the row', () => {
  let pinia: ReturnType<typeof createPinia>

  beforeEach(() => {
    pinia = createPinia()
    setActivePinia(pinia)
    apiGet.mockReset()
    apiPost.mockReset()
    useProjectStore().activeWorkspace = 'personal'
  })

  afterEach(() => {
    document.body.innerHTML = ''
    vi.restoreAllMocks()
  })

  const person = row({ id: 'p', kind: 'people', target: 'Mo', text: 'Leads the pilot.' })
  const decision = row({ id: 'd', kind: 'learnings', text: 'Order OCR moves out.' })
  const fold = row({ id: 'f', kind: 'project', target: 'Projects/Ciao.md', text: 'Ships on Fridays.' })
  const region = row({ id: 'm', kind: 'memory', region: 'memory', text: 'Prefers short PRs.' })

  const PREVIEWS: Record<string, ProposalPreview> = {
    p: preview({
      id: 'p', kind: 'people', action: 'write_people_note', operation: 'add',
      destination: 'People/Mo.md', before: '', after: '---\ntags: [person]\n---\n# Mo\n\nLeads the pilot.\n',
      separator: '\n', revision: 'rev-p', text: 'Leads the pilot.',
    }),
    d: preview({
      id: 'd', kind: 'learnings', action: 'append_learnings', operation: 'add',
      destination: 'Workspace/Learnings.md',
      before: '# Learnings\n## Decisions\n- one\n',
      after: '# Learnings\n## Decisions\n- one\n- Order OCR moves out.\n',
      separator: '\n', revision: 'rev-d', text: 'Order OCR moves out.',
    }),
    f: preview({
      id: 'f', kind: 'project', action: 'fold_doc', operation: 'update', exact: false,
      destination: 'Projects/Ciao.md', before: '# Ciao\n', after: '', text: 'Ships on Fridays.',
      separator: '\n', revision: 'rev-f', reason: 'a model folds this into the doc when you accept',
    }),
    m: preview({ id: 'm', revision: 'rev-m', text: 'Prefers short PRs.' }),
  }

  function mockQueue(rows: ProposalRow[]) {
    apiGet.mockImplementation((url: string) => {
      if (url.startsWith('/api/proposals/history')) return Promise.resolve({ rows: [], total: 0, truncated: false })
      const m = /^\/api\/proposals\/([^/]+)\/preview/.exec(url)
      if (m) return Promise.resolve({ ok: true, preview: PREVIEWS[m[1]] })
      return Promise.resolve({ rows })
    })
  }

  function rowFor(wrapper: ReturnType<typeof mount>, text: string) {
    return wrapper.findAll('.pr-row').find(r => r.text().includes(text))!
  }

  it('fetches every row preview without a click and names each change', async () => {
    mockQueue([person, decision, fold, region])
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    for (const id of ['p', 'd', 'f', 'm']) {
      expect(apiGet).toHaveBeenCalledWith(`/api/proposals/${id}/preview`)
    }

    const p = rowFor(wrapper, 'Leads the pilot.')
    expect(p.get('.pr-op').text()).toBe('New note')
    expect(p.get('.pr-dest').text()).toBe('People/Mo.md')
    expect(p.get('.pr-where').text()).toBe('· does not exist yet')
    expect(p.get('.pr-diff .mr-box-head').text()).toBe('New file, 6 lines')
    expect(p.findAll('.pr-diff .mr-box-line--added')).toHaveLength(6)
    expect(p.get('.pr-row-accept').text()).toBe('Create note')

    const d = rowFor(wrapper, 'Order OCR moves out.')
    expect(d.get('.pr-op').text()).toBe('Add to a note')
    expect(d.get('.pr-where').text()).toBe('· under ## Decisions')
    expect(d.get('.pr-diff .mr-box-head').text()).toBe('1 line added after line 3')
    expect(d.findAll('.pr-diff .mr-box-line').map(l => l.get('.mr-box-num').text())).toEqual(['3', '4'])
    expect(d.get('.pr-row-accept').text()).toBe('Add line')

    const f = rowFor(wrapper, 'Ships on Fridays.')
    expect(f.get('.pr-op').text()).toBe('Merge into a note')
    expect(f.get('.pr-where').text()).toContain('a model folds it in when you accept')
    expect(f.get('.pr-diff').text()).toContain('Wording decided when you accept.')
    expect(f.get('.pr-row-accept').text()).toBe('Merge')

    const m = rowFor(wrapper, 'Prefers short PRs.')
    expect(m.get('.pr-op').text()).toBe('Add to a note')
    expect(m.get('.pr-dest').text()).toBe('personal/AGENTS.md')
    expect(m.get('.pr-where').text()).toBe('· Agent memory, always loaded')
    expect(m.get('.pr-row-accept').text()).toBe('Add entry')

    // Every row is the same routine choice: no pink primary on any of them.
    expect(wrapper.findAll('.pr-row .btn-primary')).toHaveLength(0)
    expect(wrapper.findAll('.pr-op-icon')).toHaveLength(4)
    wrapper.unmount()
  })

  it('accepts from the row, pinned to the revision the row shows', async () => {
    mockQueue([person])
    apiPost.mockResolvedValue({} as never)
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    await wrapper.get('.pr-row-accept').trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith('/api/proposals/p/accept', { expected_revision: 'rev-p' })
    wrapper.unmount()
  })

  it('shows a conflict on the row with the destination as it is now', async () => {
    mockQueue([decision])
    apiPost.mockRejectedValue(Object.assign(new Error('the destination changed'), {
      payload: {
        conflict: true,
        preview: { ...PREVIEWS.d, revision: 'rev-d2', before: '# Learnings\n## Decisions\n- one\n- two\n', after: '# Learnings\n## Decisions\n- one\n- two\n- Order OCR moves out.\n' },
      },
    }))
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    await wrapper.get('.pr-row-accept').trigger('click')
    await flushPromises()
    expect(wrapper.get('.pr-row').text()).toContain('The destination changed since this preview')
    expect(wrapper.get('.pr-diff .mr-box-head').text()).toBe('1 line added after line 4')
    expect(useProposalsStore().previews.d.revision).toBe('rev-d2')
    wrapper.unmount()
  })

  it('filters by change type, one chip per type present', async () => {
    mockQueue([person, decision, region, fold])
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    const chips = () => wrapper.findAll('.pr-chips button')
    expect(chips().map(c => c.text())).toEqual(['All 4', 'New note 1', 'Add to a note 2', 'Merge 1'])
    await chips()[2].trigger('click')
    expect(wrapper.findAll('.pr-row')).toHaveLength(2)
    expect(chips()[2].attributes('aria-pressed')).toBe('true')

    // A batch only reaches what the chip is showing.
    await wrapper.get('.pr-select-toggle').trigger('click')
    await wrapper.get('.pr-group-select input').trigger('change')
    expect(wrapper.get('.pr-batch-count').text()).toBe('2 selected')
    wrapper.unmount()
  })

  it('keeps a row readable when its preview could not be read, with a retry', async () => {
    apiGet.mockImplementation((url: string) => {
      if (url.startsWith('/api/proposals/history')) return Promise.resolve({ rows: [], total: 0, truncated: false })
      if (url.includes('/preview')) return Promise.reject(new Error('boom'))
      return Promise.resolve({ rows: [person] })
    })
    const wrapper = mount(ProposalReviewPanel, { global: { plugins: [pinia] } })
    await flushPromises()

    const sub = wrapper.get('.pr-row-sub')
    expect(sub.text()).toContain('could not read the destination')
    expect(wrapper.get('.pr-row-action').text()).toBe('Review')
    apiGet.mockImplementation((url: string) => (url.includes('/preview')
      ? Promise.resolve({ ok: true, preview: PREVIEWS.p })
      : Promise.resolve({ rows: [person] })))
    await sub.get('.pr-preview-retry').trigger('click')
    await flushPromises()
    expect(wrapper.get('.pr-op').text()).toBe('New note')
    wrapper.unmount()
  })
})
