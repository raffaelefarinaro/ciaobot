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
import ProjectSidebar from '../ProjectSidebar.vue'
import { useProjectStore } from '../../stores/projects'
import { useProposalsStore } from '../../stores/proposals'
import type { ProposalRow } from '../../lib/types'

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
      { name: 'personal', vault_root: '/tmp/p', default_provider: 'claude', default_model: 'sonnet', gws_profile: '' },
      { name: 'work', vault_root: '/tmp/w', default_provider: 'claude', default_model: 'sonnet', gws_profile: '' },
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

    expect(wrapper.get('a[href="/memory"] .nav-item-badge--count').text()).toBe('4')
    const workspaceToggle = wrapper.findAll('.workspace-toggle').find(toggle => !toggle.classes().includes('view-toggle'))
    expect(workspaceToggle).toBeTruthy()
    expect(workspaceToggle!.findAll('button')[0].text()).toContain('3')
    expect(workspaceToggle!.findAll('button')[1].text()).toContain('1')

    const stats = wrapper.findAll('.mm-stat').map(s => s.text())
    expect(stats[0]).toContain('3')       // shown
    expect(stats[0]).toContain('of 3')
    expect(stats[2]).toContain('1')       // the work row
    expect(stats[2]).toContain('other workspaces')
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

    expect(wrapper.text()).not.toContain('other workspaces')
  })
})
