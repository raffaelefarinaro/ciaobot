// @vitest-environment jsdom

/**
 * Memory's sections live in the sidebar, the way Settings' tabs do.
 *
 * The page used to carry a Review/Map switch and a tab row of its own while
 * Settings listed its sections here — two navigation patterns for one app.
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

    // No stat tiles and no proposal filters: the section list carries the
    // counts, and Suggested filters itself.
    expect(wrapper.findAll('.mm-stat')).toHaveLength(0)
    expect(wrapper.find('.mm-search').exists()).toBe(false)
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

  it('lists Memory\'s sections like Settings\' tabs, each a route with its count', async () => {
    const vaultReview = useVaultReviewStore()
    vaultReview.loadedWorkspace = 'personal'
    vaultReview.candidates = [
      { candidate_id: 'c1', workspace: 'personal', path: 'a.md', content_hash: 'h1', signals: ['unverified'], priority: 0, evidence: EVIDENCE, status: 'candidate', disposition: '', deferred_until: '' },
    ]
    const wrapper = await mountSidebar()

    const nav = wrapper.get('nav[aria-label="Memory sections"]')
    expect(nav.findAll('.sidebar-list-label').map(h => h.text())).toEqual(['To decide', 'Explore', 'Records'])
    const items = nav.findAll('.memory-nav-item')
    expect(items.map(i => i.attributes('href'))).toEqual([
      '/memory/suggested', '/memory/revisit', '/memory/map', '/memory/retired', '/memory/history',
    ])
    // Three proposals in `personal`, one note to revisit; the queues' counts
    // are the accent ones, and the accessible name carries the number.
    expect(items[0].get('.memory-nav-count').text()).toBe('3')
    expect(items[0].get('.memory-nav-count').classes()).toContain('memory-nav-count--due')
    expect(items[1].attributes('aria-label')).toBe('To revisit, 1 waiting')
    // Nothing retired: no zero badge.
    expect(items[3].find('.memory-nav-count').exists()).toBe(false)
  })

  it('shows no map count until the selected workspace\'s graph has loaded', async () => {
    // Switching to an uncached workspace leaves the previous graph's nodes in
    // place while the new one loads; its count must not appear under the new
    // workspace.
    const mm = useMemoryMapStore()
    mm.nodes = [{ id: 'n', title: 'n', type: 'note', tags: [], aliases: [], description: '', workspace: 'work', degree: 0, mtime: 0, updated: '', stale: false, ageDays: null, x: 0, y: 0, vx: 0, vy: 0 }] as never
    mm.loadedWorkspace = 'work'
    const wrapper = await mountSidebar()
    const map = wrapper.findAll('.memory-nav-item')[2]
    expect(map.find('.memory-nav-count').exists()).toBe(false)

    mm.loadedWorkspace = 'personal'
    await nextTick()
    expect(map.get('.memory-nav-count').text()).toBe('1')
  })

  it('marks the section on screen as the current page', async () => {
    useMemoryMapStore().setSection('revisit')
    const wrapper = await mountSidebar()
    const current = wrapper.get('.memory-nav-item[aria-current="page"]')
    expect(current.text()).toContain('To revisit')
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

  it('shows the map\'s search and categories only under Map', async () => {
    // They filter the map and nothing else; beside a review queue they would
    // filter nothing on screen.
    const mm = useMemoryMapStore()
    mm.setSection('revisit')
    const wrapper = await mountSidebar()
    expect(wrapper.find('.mm-search').exists()).toBe(false)

    mm.setSection('map')
    await nextTick()
    expect(wrapper.find('.mm-search').exists()).toBe(true)
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
