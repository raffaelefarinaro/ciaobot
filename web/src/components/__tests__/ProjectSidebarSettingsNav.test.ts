// @vitest-environment jsdom

/** The Settings rail is two labelled groups: Ciaobot, then the active workspace. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { mount } from '@vue/test-utils'
import ProjectSidebar from '../ProjectSidebar.vue'
import { useProjectStore } from '../../stores/projects'
import { workspaceLabel } from '../../lib/workspaceLabel'
import { SETTINGS_NAV_GROUPS } from '../../lib/settingsNav'

vi.mock('../../lib/api', () => ({
  api: { get: vi.fn().mockResolvedValue({ rows: [] }), post: vi.fn(), patch: vi.fn(), del: vi.fn() },
}))

async function mountSettingsSidebar(path: string) {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/:pathMatch(.*)*', component: { template: '<div />' } }],
  })
  await router.push(path)
  await router.isReady()
  return mount(ProjectSidebar, {
    attachTo: document.body,
    props: { collapsed: false, mode: 'settings' },
    global: { plugins: [router] },
  })
}

describe('ProjectSidebar settings nav', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    const store = useProjectStore()
    store.workspaces = [
      { name: 'personal', vault_root: '/tmp/p', default_provider: 'claude', gws_profile: '' },
      { name: 'work', vault_root: '/tmp/w', default_provider: 'claude', gws_profile: '', color: 'violet' },
    ]
    store.activeWorkspace = 'work'
  })

  afterEach(() => {
    document.body.innerHTML = ''
    vi.restoreAllMocks()
  })

  it('renders two labelled groups', async () => {
    const wrapper = await mountSettingsSidebar('/settings/providers')
    const groups = wrapper.findAll('.settings-nav-list [role="group"]')
    expect(groups).toHaveLength(2)

    const headings = groups.map(g => g.find('h2'))
    expect(headings[0].text()).toBe('Ciaobot')
    expect(headings[1].text()).toContain(workspaceLabel('work'))
    const mark = headings[1].find('.workspace-scope-mark')
    expect(mark.exists()).toBe(true)
    expect(mark.text()).toBe('2')
    expect(headings[1].attributes('data-workspace-color')).toBeTruthy()
    expect(headings.map(h => h.text())).not.toContain('Settings')

    groups.forEach((group, i) => {
      expect(group.attributes('aria-labelledby')).toBe(headings[i].attributes('id'))
      const hrefs = group.findAll('a').map(a => a.attributes('href'))
      expect(hrefs).toEqual(SETTINGS_NAV_GROUPS[i].items.map(item => item.to))
    })

    const active = wrapper.findAll('a.settings-nav-item.active')
    expect(active).toHaveLength(1)
    expect(active[0].attributes('href')).toBe('/settings/providers')
    expect(active[0].attributes('aria-current')).toBe('page')
    wrapper.unmount()
  })
})
