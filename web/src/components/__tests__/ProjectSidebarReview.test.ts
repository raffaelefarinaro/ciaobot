// @vitest-environment jsdom

/**
 * The review queue's controls live in the sidebar, like the memory map's.
 *
 * They used to be a segmented control in the panel header while this column sat
 * empty — the only memory view that kept its controls somewhere else.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import ProjectSidebar from '../ProjectSidebar.vue'
import { useProjectStore } from '../../stores/projects'
import { useProposalsStore } from '../../stores/proposals'
import { useVaultReviewStore } from '../../stores/vaultReview'
import { useMemoryMapStore } from '../../stores/memoryMap'
import type { ProposalRow, VaultReviewCandidate } from '../../lib/types'

const EVIDENCE: VaultReviewCandidate['evidence'] = {
  backlinks: [], outbound_links: [], bridge: false, duplicate_group: [],
  last_update: '', type: 'note', age_days: null,
}

vi.mock('../../lib/api', () => ({
  api: { get: vi.fn().mockResolvedValue({ rows: [] }), post: vi.fn(), patch: vi.fn(), del: vi.fn() },
}))

function row(overrides: Partial<ProposalRow> = {}): ProposalRow {
  return {
    id: 'r1',
    kind: 'memory',
    text: 'Remember the thing',
    source: '',
    workspace: 'personal',
    path: 'personal/Workspace/Memory-Proposals.md',
    line: 3,
    ...overrides,
  }
}

async function mountSidebar() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/', component: { template: '<div />' } }],
  })
  await router.push('/')
  await router.isReady()
  return mount(ProjectSidebar, {
    attachTo: document.body,
    props: { collapsed: false, mode: 'proposals' },
    global: { plugins: [router] },
  })
}

describe('ProjectSidebar review section', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    const store = useProjectStore()
    store.workspaces = [
      { name: 'personal', vault_root: '/tmp/p', default_provider: 'claude', gws_profile: '' },
      { name: 'work', vault_root: '/tmp/w', default_provider: 'claude', gws_profile: '' },
    ]
    store.activeWorkspace = 'personal'
    const proposals = useProposalsStore()
    proposals.rows = [
      row({ id: 'p-mem' }),
      row({ id: 'p-mem-2' }),
      row({ id: 'p-skill', kind: 'skill' }),
      row({ id: 'w-mem', workspace: 'work' }),
    ]
  })

  afterEach(() => {
    document.body.innerHTML = ''
    vi.restoreAllMocks()
  })

  it('counts the queue for the active workspace and what sits elsewhere', async () => {
    const wrapper = await mountSidebar()

    // The count lives on the memory pulse, not on the nav: the link only
    // states it in its accessible name.
    // A subtle count on the section itself, scoped to this workspace.
    const memoryLink = wrapper.get('a[href="/memory"]')
    expect(memoryLink.attributes('data-count')).toBe('3')
    expect(memoryLink.attributes('aria-label')).toBe('memory — 3 to review in this workspace')
    // The workspace scope is one dropdown at the top of the rail now, not a
    // per-mode row of pills.
    const workspaceTrigger = wrapper.get('.workspace-scope-trigger')
    expect(workspaceTrigger.text()).toContain('Personal')
    await workspaceTrigger.trigger('click')
    // No counts at the workspace level: the options carry name and key only.
    const workspaceOptions = wrapper.findAll('.workspace-scope-option')
    expect(workspaceOptions[0].find('.badge').exists()).toBe(false)
    expect(workspaceOptions[1].find('.badge').exists()).toBe(false)

    // No stat tiles: the Suggested tab counts the queue and the batch bar
    // counts the selection. The sidebar keeps only what filters the list.
    expect(wrapper.findAll('.mm-stat')).toHaveLength(0)
    expect(wrapper.find('.mm-search input').attributes('placeholder')).toBe('Search proposals…')
  })

  it('marks workspaces with their key number and offers a new workspace', async () => {
    const wrapper = await mountSidebar()
    await wrapper.get('.workspace-scope-trigger').trigger('click')
    const options = wrapper.findAll('.workspace-scope-option')
    // Number tiles replace both the initial and the keycap on the right.
    expect(options[0].get('.workspace-scope-mark').text()).toBe('1')
    expect(options[1].get('.workspace-scope-mark').text()).toBe('2')
    expect(wrapper.find('.workspace-scope-menu .sidebar-keycap').exists()).toBe(false)
    const create = wrapper.get('.workspace-scope-new')
    expect(create.text()).toContain('New workspace')
    expect(create.attributes('href')).toBe('/settings/workspaces#new-workspace')
  })

  it('opens the workspace scope as a keyboard menu and restores focus', async () => {
    const wrapper = await mountSidebar()
    const trigger = wrapper.get<HTMLButtonElement>('.workspace-scope-trigger')

    await trigger.trigger('click')
    await nextTick()
    const menu = wrapper.get('.workspace-scope-menu')
    const options = menu.findAll<HTMLButtonElement>('[role="menuitem"]')
    expect(document.activeElement).toBe(options[0].element)

    await menu.trigger('keydown', { key: 'ArrowDown' })
    expect(document.activeElement).toBe(options[1].element)

    await menu.trigger('keydown', { key: 'Escape' })
    await nextTick()
    expect(wrapper.find('.workspace-scope-menu').exists()).toBe(false)
    expect(document.activeElement).toBe(trigger.element)
    wrapper.unmount()
  })

  it('offers a kind row per kind, counted over the scope', async () => {
    const wrapper = await mountSidebar()

    const labels = wrapper.findAll('.mm-link-item').map(i => i.text())
    expect(labels.some(t => t.startsWith('all') && t.includes('3'))).toBe(true)
    expect(labels.some(t => t.startsWith('memory') && t.includes('2'))).toBe(true)
    expect(labels.some(t => t.startsWith('skill') && t.includes('1'))).toBe(true)
  })

  it('clicking a kind filters the shared store, and reset clears it', async () => {
    const wrapper = await mountSidebar()
    const proposals = useProposalsStore()

    const skill = wrapper.findAll('.mm-link-item').find(i => i.text().startsWith('skill'))
    await skill!.trigger('click')

    expect(proposals.kindFilter).toBe('skill')
    expect(proposals.visibleRows('personal').map(r => r.id)).toEqual(['p-skill'])

    await wrapper.find('.mm-link').trigger('click')   // "reset"
    expect(proposals.kindFilter).toBe('all')
  })

  it('keeps every kind listed and counted while one is filtered', async () => {
    // The chips are how you switch back, so filtering must not remove them, and
    // their counts must not renumber under the pointer.
    const proposals = useProposalsStore()
    proposals.kindFilter = 'skill'

    const wrapper = await mountSidebar()

    const labels = wrapper.findAll('.mm-link-item').map(i => i.text())
    expect(labels.some(t => t.startsWith('memory') && t.includes('2'))).toBe(true)
    expect(labels.some(t => t.startsWith('skill') && t.includes('1'))).toBe(true)
    expect(labels.some(t => t.startsWith('all') && t.includes('3'))).toBe(true)
  })

  it('leaves the Review/Map switch to the Memory page header', async () => {
    const wrapper = await mountSidebar()
    expect(wrapper.find('.view-toggle').exists()).toBe(false)
  })

  it('counts both review queues on the Memory section, not just the proposals one', async () => {
    // "Is there anything to decide here" has two queues behind it. Counting
    // one of them said "3" beside two more notes waiting on the other tab.
    const vaultReview = useVaultReviewStore()
    vaultReview.loadedWorkspace = 'personal'
    vaultReview.candidates = [
      { candidate_id: 'c1', workspace: 'personal', path: 'a.md', content_hash: 'h1', signals: ['unlinked'], priority: 1, evidence: EVIDENCE, status: 'candidate', disposition: '', deferred_until: '' },
      { candidate_id: 'c2', workspace: 'personal', path: 'b.md', content_hash: 'h2', signals: ['unlinked'], priority: 1, evidence: EVIDENCE, status: 'candidate', disposition: '', deferred_until: '' },
    ]

    const wrapper = await mountSidebar()
    // Three proposals in `personal` plus two notes to revisit.
    expect(wrapper.get('a[href="/memory"]').attributes('data-count')).toBe('5')
    expect(wrapper.get('a[href="/memory"]').attributes('aria-label'))
      .toBe('memory — 5 to review in this workspace')
  })

  it('leaves retired notes out of that count', async () => {
    // Records, not work: a badge counting them asks for attention no click
    // can clear.
    const vaultReview = useVaultReviewStore()
    vaultReview.loadedWorkspace = 'personal'
    vaultReview.candidates = []
    vaultReview.trashed = [
      { candidate_id: 't1', workspace: 'personal', original_path: 'old.md', content_hash: 'h3', trashed_at: '2026-09-01T00:00:00Z' },
    ]

    const wrapper = await mountSidebar()
    expect(wrapper.get('a[href="/memory"]').attributes('data-count')).toBe('3')
  })

  it('shows no proposal filters while Notes to revisit is open', async () => {
    // Search and kinds act on the suggestions list only; beside the
    // retirement queue they would filter nothing on screen.
    const mm = useMemoryMapStore()
    mm.reviewTab = 'retirement'

    const wrapper = await mountSidebar()

    expect(wrapper.find('.mm-search').exists()).toBe(false)
    expect(wrapper.findAll('.mm-stat')).toHaveLength(0)
  })

  it('does not render the review section for other modes', async () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: { template: '<div />' } }],
    })
    await router.push('/')
    await router.isReady()
    const wrapper = mount(ProjectSidebar, {
      attachTo: document.body,
      props: { collapsed: false, mode: 'settings' },
      global: { plugins: [router] },
    })

    expect(wrapper.find('.mm-search').exists()).toBe(false)
  })
})
