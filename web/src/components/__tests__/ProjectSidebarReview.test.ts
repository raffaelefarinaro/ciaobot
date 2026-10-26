// @vitest-environment jsdom

/**
 * Memory's sections live in the sidebar, the way Settings' tabs do.
 *
 * The page used to carry a Review/Map switch and a tab row of its own while
 * Settings listed its sections here — two navigation patterns for one app.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Mock } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import ProjectSidebar from '../ProjectSidebar.vue'
import { useProjectStore } from '../../stores/projects'
import { api } from '../../lib/api'
import { useProposalsStore } from '../../stores/proposals'
import { useVaultReviewStore } from '../../stores/vaultReview'
import { useMemoryMapStore } from '../../stores/memoryMap'
import { useNotificationInboxStore } from '../../stores/notificationInbox'
import { useHousekeepingStore } from '../../stores/housekeeping'
import { useTaskStore } from '../../stores/tasks'
import { useTaskSignalsStore } from '../../stores/taskSignals'
import type { SourceHealth } from '../../lib/sourceHealth'
import type { ProposalRow, VaultReviewCandidate } from '../../lib/types'

const apiGet = api.get as unknown as Mock

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

async function mountSidebar(
  mode: 'chat' | 'memory' | 'proposals' = 'proposals',
  extra: { collapsed?: boolean } = {},
) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { template: '<div />' } },
    ],
  })
  await router.push('/')
  await router.isReady()
  return mount(ProjectSidebar, {
    attachTo: document.body,
    props: { collapsed: extra.collapsed ?? false, mode },
    global: { plugins: [router] },
  })
}

describe('ProjectSidebar review section', () => {
  it('waits for the primary view to paint before loading review badges', async () => {
    const store = useProjectStore()
    useProposalsStore().loaded = false
    useProposalsStore().rows = []
    useVaultReviewStore().loadedWorkspace = null
    const wrapper = await mountSidebar()
    expect(apiGet.mock.calls.map(([path]) => path)).not.toContain('/api/proposals')
    expect(apiGet.mock.calls.some(([path]) => path.startsWith('/api/vault/review'))).toBe(false)

    store.bootstrapped = true
    await vi.waitFor(() => {
      expect(apiGet).toHaveBeenCalledWith('/api/proposals')
      expect(apiGet.mock.calls.some(([path]) => path.startsWith('/api/vault/review?workspace=personal'))).toBe(true)
    }, { timeout: 2000 })
    wrapper.unmount()
  })

  beforeEach(() => {
    apiGet.mockClear()
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

  it('lists Workspace settings, Manage workspaces and New workspace after the options', async () => {
    const wrapper = await mountSidebar()
    await wrapper.get('.workspace-scope-trigger').trigger('click')
    const menu = wrapper.get('.workspace-scope-menu')
    const items = menu.findAll('[role="menuitem"]')
    expect(items.slice(-3).map(i => i.text().replace(/^\+\s*/, ''))).toEqual(
      ['Workspace settings', 'Manage workspaces', 'New workspace'],
    )
    expect(items.slice(-3).map(i => i.attributes('href'))).toEqual(
      ['/settings/workspace', '/settings/workspaces', '/settings/workspaces#new-workspace'],
    )
    // A separator sits between the workspace options and the three entries.
    const children = Array.from(menu.element.children)
    const separator = children.findIndex(el => el.getAttribute('role') === 'separator')
    expect(separator).toBeGreaterThan(0)
    expect(children[separator + 1]).toBe(items[items.length - 3].element)
    expect(children[separator - 1]).toBe(items[items.length - 4].element)
    wrapper.unmount()
  })

  it('arrow keys reach the settings entries', async () => {
    const wrapper = await mountSidebar()
    const trigger = wrapper.get<HTMLButtonElement>('.workspace-scope-trigger')
    await trigger.trigger('click')
    await nextTick()
    const menu = wrapper.get('.workspace-scope-menu')
    const items = menu.findAll<HTMLElement>('[role="menuitem"]')

    await menu.trigger('keydown', { key: 'End' })
    expect(document.activeElement).toBe(items[items.length - 1].element)
    expect(items[items.length - 1].text()).toContain('New workspace')
    await menu.trigger('keydown', { key: 'ArrowUp' })
    expect(document.activeElement).toBe(items[items.length - 2].element)
    expect(items[items.length - 2].text()).toBe('Manage workspaces')
    await menu.trigger('keydown', { key: 'End' })
    await menu.trigger('keydown', { key: 'ArrowDown' })
    expect(document.activeElement).toBe(items[0].element)

    await menu.trigger('keydown', { key: 'Escape' })
    await nextTick()
    expect(wrapper.find('.workspace-scope-menu').exists()).toBe(false)
    expect(document.activeElement).toBe(trigger.element)
    wrapper.unmount()
  })

  it('choosing a settings entry closes the menu', async () => {
    const wrapper = await mountSidebar()
    await wrapper.get('.workspace-scope-trigger').trigger('click')
    const manage = wrapper.findAll('[role="menuitem"]').find(i => i.text() === 'Manage workspaces')
    expect(manage).toBeTruthy()
    await manage!.trigger('click')
    await nextTick()
    expect(wrapper.find('.workspace-scope-menu').exists()).toBe(false)
    wrapper.unmount()
  })

  it.each([
    ['chat', true],
    ['memory', false],
  ] as const)('switches workspace from the %s view with transition %s', async (mode, transition) => {
    const store = useProjectStore()
    const switchWorkspace = vi.spyOn(store, 'switchWorkspace').mockResolvedValue(true)
    const wrapper = await mountSidebar(mode)
    await wrapper.get('.workspace-scope-trigger').trigger('click')
    await wrapper.findAll('.workspace-scope-option')[1].trigger('click')
    // From a chat the switch lands on the new workspace's Home; a view with
    // no chat to leave stays put. Neither opens one of its chats.
    expect(switchWorkspace).toHaveBeenCalledWith('work', { transition })
    wrapper.unmount()
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
      { candidate_id: 'c1', workspace: 'personal', path: 'a.md', content_hash: 'h1', signals: ['unverified'], priority: 0, evidence: EVIDENCE, status: 'candidate', disposition: '', deferred_until: '', completable: false, completion_moves_folder: false },
    ]
    const wrapper = await mountSidebar()

    const nav = wrapper.get('nav[aria-label="Memory sections"]')
    expect(nav.findAll('.sidebar-list-label').map(h => h.text())).toEqual(['Explore', 'Records'])
    const items = nav.findAll('.memory-nav-item')
    expect(items.map(i => i.attributes('href'))).toEqual([
      '/memory/review', '/memory/map', '/memory/categories', '/memory/import',
      '/memory/retired', '/memory/history',
    ])
    // Explore holds the map, then the list of what the map is drawing, then the
    // way conversations get in.
    expect(items[1].text()).toContain('Map')
    expect(items[2].text()).toContain('Categories')
    expect(items[3].text()).toContain('Import')
    // The category list is configuration, not a queue: no count on it. Import has
    // none either — nothing is listed until the reader asks for a scan.
    expect(items[2].find('.memory-nav-count').exists()).toBe(false)
    expect(items[3].find('.memory-nav-count').exists()).toBe(false)
    // Three proposals in `personal` plus one note to revisit, on the one
    // To decide row; its count is the accent one, and the accessible name
    // carries the number.
    expect(items[0].get('.memory-nav-count').text()).toBe('4')
    expect(items[0].get('.memory-nav-count').classes()).toContain('memory-nav-count--due')
    expect(items[0].attributes('aria-label')).toBe('To decide, 4 waiting')
    // Nothing retired: no zero badge.
    expect(items[4].find('.memory-nav-count').exists()).toBe(false)
  })

  it('offers a Manage link from the map\'s category chips to the category list', async () => {
    // The chips filter the drawing; the list of kinds the vault keeps is a
    // different thing, and it is editable.
    useMemoryMapStore().setSection('map')
    const wrapper = await mountSidebar()
    const manage = wrapper.get('.mm-row-links a')
    expect(manage.text()).toBe('Manage')
    expect(manage.attributes('href')).toBe('/memory/categories')
  })

  it('shows no map count until the selected workspace\'s graph has loaded', async () => {
    // Switching to an uncached workspace leaves the previous graph's nodes in
    // place while the new one loads; its count must not appear under the new
    // workspace.
    const mm = useMemoryMapStore()
    mm.nodes = [{ id: 'n', title: 'n', type: 'note', tags: [], aliases: [], description: '', workspace: 'work', degree: 0, mtime: 0, updated: '', stale: false, ageDays: null, x: 0, y: 0, vx: 0, vy: 0 }] as never
    mm.loadedWorkspace = 'work'
    const wrapper = await mountSidebar()
    const map = wrapper.findAll('.memory-nav-item')[1]
    expect(map.find('.memory-nav-count').exists()).toBe(false)

    mm.loadedWorkspace = 'personal'
    await nextTick()
    expect(map.get('.memory-nav-count').text()).toBe('1')
  })

  it('marks the section on screen as the current page', async () => {
    useMemoryMapStore().setSection('review')
    const wrapper = await mountSidebar()
    const current = wrapper.get('.memory-nav-item[aria-current="page"]')
    expect(current.text()).toContain('To decide')
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
      { candidate_id: 'c1', workspace: 'personal', path: 'a.md', content_hash: 'h1', signals: ['unlinked'], priority: 1, evidence: EVIDENCE, status: 'candidate', disposition: '', deferred_until: '', completable: false, completion_moves_folder: false },
      { candidate_id: 'c2', workspace: 'personal', path: 'b.md', content_hash: 'h2', signals: ['unlinked'], priority: 1, evidence: EVIDENCE, status: 'candidate', disposition: '', deferred_until: '', completable: false, completion_moves_folder: false },
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
    mm.setSection('review')
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

const READY_AT = '2026-10-26T10:00:00.000Z'
const readyHealth = (): SourceHealth => ({ status: 'ready', lastSuccessAt: READY_AT, error: '' })

// Real stores, seeded the way the inbox store reads them: N unread chats in the
// one personal project, every source ready.
function seedInbox(unread: number) {
  const projects = useProjectStore()
  projects.projects = [
    { project_id: 'p-personal', name: 'General', workspace: 'personal' },
  ] as unknown as typeof projects.projects
  projects.activeWorkspace = 'personal'
  projects.activeChatId = null
  projects.chats = Array.from({ length: unread }, (_, i) => ({
    chat_id: `c${i}`,
    project_id: 'p-personal',
    title: `Chat ${i}`,
    archived: false,
    local: true,
    last_activity_at: '2026-10-26T09:00:00Z',
    last_read_at: '',
  })) as unknown as typeof projects.chats
  projects.chatsHealth = readyHealth()
  const signals = useTaskSignalsStore()
  signals.healthWorkspace = 'personal'
  signals.health = readyHealth()
  const housekeeping = useHousekeepingStore()
  housekeeping.actionsHealth = readyHealth()
  housekeeping.updateTasksWorkspace = 'personal'
  housekeeping.updateTasksHealth = readyHealth()
  useTaskStore().schedulesLoaded = true
  useProposalsStore().rows = []
  useProposalsStore().loaded = true
  useVaultReviewStore().loadedWorkspace = 'personal'
}

describe('notifications bell', () => {
  beforeEach(() => {
    apiGet.mockClear()
    setActivePinia(createPinia())
    const store = useProjectStore()
    store.workspaces = [
      { name: 'personal', vault_root: '/tmp/p', default_provider: 'claude', gws_profile: '' },
    ]
    store.activeWorkspace = 'personal'
  })

  afterEach(() => {
    document.body.innerHTML = ''
    vi.restoreAllMocks()
  })

  it('zero_count_has_no_numeral_and_plain_name', async () => {
    seedInbox(0)
    const wrapper = await mountSidebar('chat')
    const bell = wrapper.get('.notif-bell')
    expect(wrapper.find('.notif-bell-count').exists()).toBe(false)
    expect(bell.attributes('aria-label')).toBe('Notifications, none')
    wrapper.unmount()
  })

  it('count_and_99_plus', async () => {
    seedInbox(3)
    const wrapper = await mountSidebar('chat')
    let bell = wrapper.get('.notif-bell')
    expect(bell.get('.notif-bell-count').text()).toBe('3')
    expect(bell.attributes('aria-label')).toBe('Notifications, 3 items')
    wrapper.unmount()

    seedInbox(100)
    const big = await mountSidebar('chat')
    bell = big.get('.notif-bell')
    expect(bell.get('.notif-bell-count').text()).toBe('99+')
    expect(bell.attributes('aria-label')).toBe('Notifications, 100 items')
    big.unmount()
  })

  it('partial_status_indicator', async () => {
    seedInbox(1)
    const projects = useProjectStore()
    projects.chatsHealth = { status: 'stale', lastSuccessAt: READY_AT, error: 'x' }
    const wrapper = await mountSidebar('chat')
    expect(wrapper.find('.notif-bell-ring').exists()).toBe(true)
    expect(wrapper.get('.notif-bell').attributes('aria-label'))
      .toBe('Notifications, 1 item. Some sources could not be fully checked')

    projects.chatsHealth = { status: 'checking', lastSuccessAt: '', error: '' }
    await nextTick()
    expect(wrapper.find('.notif-bell-ring').exists()).toBe(false)
    wrapper.unmount()
  })

  it('collapsed_rail_has_the_bell', async () => {
    seedInbox(100)
    const wrapper = await mountSidebar('chat', { collapsed: true })
    expect(wrapper.find('.nav-links').exists()).toBe(false)
    const bell = wrapper.get('.notif-bell')
    expect(bell.attributes('aria-label')).toBe('Notifications, 100 items')
    expect(bell.get('.notif-bell-count').text()).toBe('99+')
    wrapper.unmount()
  })

  it('sidebar_never_calls_inbox_reads', async () => {
    seedInbox(1)
    const inbox = useNotificationInboxStore()
    const init = vi.spyOn(inbox, 'init')
    const refresh = vi.spyOn(inbox, 'refresh')
    const retry = vi.spyOn(inbox, 'retry')
    const wrapper = await mountSidebar('chat')
    await wrapper.get('.notif-bell').trigger('click')
    expect(init).not.toHaveBeenCalled()
    expect(refresh).not.toHaveBeenCalled()
    expect(retry).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})
