// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { defineComponent, h, nextTick } from 'vue'
import { api } from '../../lib/api'
import SettingsView from '../SettingsView.vue'

afterEach(() => {
  vi.restoreAllMocks()
})

const ROUTINES = { workspace_context: { workspace_root: '/srv/ciao' } }

function stubGet(routines: unknown = {}) {
  return vi.spyOn(api, 'get').mockImplementation(async (url: string) => {
    if (url === '/api/settings/routines') return routines as never
    if (url === '/api/auth/settings') return { password_configured: true } as never
    if (url === '/api/local/status') return { git_repo: true, branch: 'main', dirty: false } as never
    if (url === '/api/workspace-move') return { workspace_root: '/srv/ciao', operation: null } as never
    if (url === '/api/agent-assets') return { subagents: [], commands: [], health: { status: 'ok', checks: [] } } as never
    if (url.startsWith('/api/proposals/handover')) {
      return {
        workspace: 'personal', handover: 'off', acting_available: true,
        agreement: {
          decided: 0, matched: 0, rate: null, window: 50, min_decisions: 20, threshold: 0.9,
          predicted_accept: 0, accept_matched: 0, predicted_dismiss: 0, dismiss_matched: 0,
          suggest_handover: false,
        },
      } as never
    }
    return {} as never
  })
}

async function mountSettings(path: string, routines?: unknown) {
  setActivePinia(createPinia())
  const stub = defineComponent({ render: () => h('div') })
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/settings', component: stub },
      { path: '/settings/:tab', component: stub },
    ],
  })
  await router.push(path)
  await router.isReady()
  const get = stubGet(routines)
  // spyOn returns the existing spy when a test mounts twice: count this mount only.
  get.mockClear()
  const wrapper = mount(SettingsView, {
    attachTo: document.body,
    global: { plugins: [router], stubs: { Teleport: true, UpdateProgressView: stub } },
  })
  await flushPromises()
  await nextTick()
  await nextTick()
  return { wrapper, router, get }
}

function titles(wrapper: Awaited<ReturnType<typeof mountSettings>>['wrapper']): string[] {
  return wrapper.findAll('.section-title').map(el => el.text())
}

function calledUrls(get: ReturnType<typeof stubGet>): string[] {
  return get.mock.calls.map(call => String(call[0]).split('?')[0])
}

describe('Settings pages', () => {
  const MOVED = ['Appearance', 'This host', 'PWA password', 'Other devices', 'Network access', 'Developer', 'Workspace health', 'Memory backup', 'Update task history', 'Memory handover']

  it('each page renders only its own cards', async () => {
    const general = await mountSettings('/settings')
    try {
      const t = titles(general.wrapper)
      for (const title of ['Updates', 'What can Ciaobot do?', 'Use Ciaobot as an app', 'Notifications', 'Keyboard shortcuts', 'Open source']) {
        expect(t).toContain(title)
      }
      for (const title of MOVED) expect(t).not.toContain(title)
    } finally {
      general.wrapper.unmount()
    }

    const appearance = await mountSettings('/settings/appearance')
    try {
      expect(titles(appearance.wrapper)).toEqual(['Appearance'])
    } finally {
      appearance.wrapper.unmount()
    }

    const access = await mountSettings('/settings/access')
    try {
      expect(titles(access.wrapper)).toEqual(['PWA password', 'Other devices', 'Network access'])
    } finally {
      access.wrapper.unmount()
    }

    const advanced = await mountSettings('/settings/advanced')
    try {
      const t = titles(advanced.wrapper)
      for (const title of ['This host', 'Memory backup', 'Workspace health', 'Developer']) expect(t).toContain(title)
      expect(t).not.toContain('Network access')
      expect(t).not.toContain('Ciaobot folder')
    } finally {
      advanced.wrapper.unmount()
    }

    const withFolder = await mountSettings('/settings/advanced', ROUTINES)
    try {
      const t = titles(withFolder.wrapper)
      expect(t).toContain('Ciaobot folder')
      expect(t).not.toContain('Main workspace')
    } finally {
      withFolder.wrapper.unmount()
    }
  })

  it('loaders follow the cards', async () => {
    const home = await mountSettings('/settings')
    try {
      const urls = calledUrls(home.get)
      expect(urls).toContain('/api/package/status')
      expect(urls).toContain('/api/settings/routines')
      expect(urls).not.toContain('/api/auth/settings')
      expect(urls).not.toContain('/api/local/status')
    } finally {
      home.wrapper.unmount()
    }

    const appearance = await mountSettings('/settings/appearance')
    try {
      const urls = calledUrls(appearance.get)
      for (const url of ['/api/auth/settings', '/api/local/status', '/api/workspace-move', '/api/package/status']) {
        expect(urls).not.toContain(url)
      }
    } finally {
      appearance.wrapper.unmount()
    }

    const access = await mountSettings('/settings/access')
    try {
      const urls = calledUrls(access.get)
      expect(urls).toContain('/api/auth/settings')
      expect(urls).toContain('/api/settings/routines')
      expect(urls).not.toContain('/api/package/status')
      expect(urls).not.toContain('/api/local/status')
    } finally {
      access.wrapper.unmount()
    }

    const advanced = await mountSettings('/settings/advanced')
    try {
      const urls = calledUrls(advanced.get)
      for (const url of ['/api/local/status', '/api/settings/routines', '/api/workspace-move', '/api/agent-assets']) {
        expect(urls).toContain(url)
      }
      expect(urls).not.toContain('/api/auth/settings')
      expect(urls).not.toContain('/api/package/status')
    } finally {
      advanced.wrapper.unmount()
    }
  })

  describe('anchors', () => {
    beforeEach(() => {
      Element.prototype.scrollIntoView = vi.fn()
    })

    it('#other-devices scrolls on the access page', async () => {
      const { wrapper } = await mountSettings('/settings/access#other-devices')
      try {
        await flushPromises()
        const spy = vi.mocked(Element.prototype.scrollIntoView)
        const targets = spy.mock.contexts.filter(el => (el as Element).id === 'other-devices')
        expect(targets).toHaveLength(1)
      } finally {
        wrapper.unmount()
      }
    })
  })
})
