// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { defineComponent, h, nextTick } from 'vue'
import { api } from '../../lib/api'
import SettingsView from '../SettingsView.vue'
import { useProjectStore } from '../../stores/projects'

afterEach(() => {
  vi.restoreAllMocks()
})

async function mountSettings(routines?: unknown, tab = '') {
  setActivePinia(createPinia())
  useProjectStore().activeWorkspace = 'work'
  const stub = defineComponent({ render: () => h('div') })
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/settings', component: stub },
      { path: '/settings/:tab', component: stub },
    ],
  })
  await router.push(`/settings${tab ? `/${tab}` : ''}`)
  await router.isReady()
  vi.spyOn(api, 'get').mockImplementation(async (url: string) => {
    if (url === '/api/settings/routines' && routines) return routines as never
    if (url === '/api/workspaces') {
      return {
        workspaces: ['personal', 'work'].map(name => ({
          name, vault_root: name, default_provider: 'claude', gws_profile: '', color: 'pink',
        })),
        active: 'work',
        primary: 'personal',
        default_agent_fs_scope: 'workspace',
        provider_options: [{ value: 'claude', label: 'Claude' }],
      } as never
    }
    if (url === '/api/integrations/gws') {
      return { installed: false, binary_path: '', cli_available: false, profiles: [] } as never
    }
    if (url.startsWith('/api/proposals/handover')) {
      return {
        workspace: 'work',
        handover: 'off',
        acting_available: true,
        agreement: {
          decided: 0, matched: 0, rate: null, window: 50, min_decisions: 20, threshold: 0.9,
          predicted_accept: 0, accept_matched: 0, predicted_dismiss: 0, dismiss_matched: 0,
          suggest_handover: false,
        },
      } as never
    }
    if (url.startsWith('/api/update-tasks')) return { tasks: [] } as never
    throw new Error('Unrelated settings data unavailable in this test')
  })
  const wrapper = mount(SettingsView, {
    attachTo: document.body,
    global: { plugins: [router], stubs: { Teleport: true, UpdateProgressView: stub } },
  })
  await flushPromises()
  await nextTick()
  await nextTick()
  return { wrapper, router }
}

it('lists the rendered sections of the tab in the On this page rail', async () => {
  const { wrapper } = await mountSettings()
  try {
    const labels = wrapper.findAll('.settings-toc-item').map(item => item.text())
    // Built from the page itself, in page order, headings in sentence case.
    // Updates leads, then the capability help, then the everyday device
    // sections, then everything else in its existing order. The workspace's own
    // Memory handover and Update task history live on Overview.
    expect(labels.slice(0, 5)).toEqual([
      'Updates',
      'What can Ciaobot do?',
      'Use Ciaobot as an app',
      'Notifications',
      'Keyboard shortcuts',
    ])
    expect(labels.slice(5)).toEqual(['Start at sign-in', 'Open source'])
    expect(labels).not.toContain('Memory handover')
    expect(labels).not.toContain('Update task history')
    expect(labels).not.toContain('Session insights')
    expect(labels).not.toContain('Appearance')
    expect(labels).not.toContain('This host')
    expect(labels.indexOf('Open source')).toBeGreaterThan(labels.indexOf('Keyboard shortcuts'))
    // The client/role callout is a notice, not a section to jump to.
    expect(labels).not.toContain('Connection role unavailable')
    // Every entry points at a real element.
    for (const item of wrapper.findAll('.settings-toc-item')) {
      expect(item.attributes('type')).toBe('button')
    }
  } finally {
    wrapper.unmount()
  }
})

it('lists Overview cards on the rail', async () => {
  const { wrapper } = await mountSettings(undefined, 'workspace')
  try {
    const labels = wrapper.findAll('.settings-toc-item').map(item => item.text())
    expect(labels).toEqual([
      'Workspace',
      'Google account',
      'Memory handover',
      'Update task history',
      'Archive this workspace',
    ])
  } finally {
    wrapper.unmount()
  }
})

it('lists Ciaobot folder on the rail, in page order, once its routines load', async () => {
  // The card only renders when the routines payload carries a workspace
  // context, which arrives after mount, so the rail has to pick it up then.
  const { wrapper } = await mountSettings({
    workspace_context: { workspace_root: '/srv/ciao' },
  }, 'advanced')
  try {
    const labels = wrapper.findAll('.settings-toc-item').map(item => item.text())
    const at = labels.indexOf('Ciaobot folder')
    expect(at).toBeGreaterThan(-1)
    expect(labels).not.toContain('Session insights')
    expect(labels[at - 1]).toBe('Memory backup')
    expect(labels[at + 1]).toBe('Workspace health')
    expect(wrapper.find('#settings-ciaobot-folder').exists()).toBe(true)
  } finally {
    wrapper.unmount()
  }
})

it('links to the feature guide and suggests asking in a chat', async () => {
  const { wrapper } = await mountSettings()
  try {
    const section = wrapper.find('#settings-what-can-ciaobot-do')
    expect(section.text()).toContain('Ask Ciaobot “What can you do?” in any chat')
    const link = section.find('a')
    expect(link.attributes('href')).toBe('https://www.raffaelefarinaro.com/ciaobot/features.html')
    expect(link.attributes('target')).toBe('_blank')
    expect(link.attributes('rel')).toContain('noopener')
    const companions = section.findAll('a')[1]
    expect(companions.attributes('href')).toBe('https://www.raffaelefarinaro.com/ciaobot/companions.html')
    expect(companions.attributes('target')).toBe('_blank')
  } finally {
    wrapper.unmount()
  }
})

it('scrolls to and focuses the section a rail entry names', async () => {
  const { wrapper } = await mountSettings()
  try {
    const scroll = vi.fn()
    Element.prototype.scrollIntoView = scroll
    const entry = wrapper.findAll('.settings-toc-item').find(item => item.text() === 'Keyboard shortcuts')
    expect(entry).toBeTruthy()
    await entry!.trigger('click')
    const section = document.getElementById('settings-keyboard-shortcuts')
    expect(section).not.toBeNull()
    expect(scroll).toHaveBeenCalled()
    expect(document.activeElement).toBe(section)
    expect(entry!.attributes('aria-current')).toBe('location')
  } finally {
    wrapper.unmount()
  }
})

it('gives Keyboard shortcuts the same labelled section heading as its neighbours', async () => {
  const { wrapper } = await mountSettings()
  try {
    const heading = wrapper.get('#keyboard-shortcuts-title')
    expect(heading.element.tagName).toBe('H2')
    expect(heading.classes()).toContain('section-title')
    const section = wrapper.get('section[aria-labelledby="keyboard-shortcuts-title"]')
    expect(section.classes()).toContain('card')
    expect(section.element.querySelector('h2#keyboard-shortcuts-title')).not.toBeNull()
  } finally {
    wrapper.unmount()
  }
})
