// @vitest-environment jsdom

// Settings > Workspaces: an assistant's File access link locates the exact
// workspace row and never acts on it. Opening the editor is navigation; the
// engine's saved value, the active workspace and the settings file stay as
// they were.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { defineComponent, h, nextTick } from 'vue'
import type { WorkspacesResponse } from '../../lib/types'

const state = vi.hoisted(() => ({
  workspaces: null as unknown,
  error: null as unknown,
  defer: false,
  pending: null as null | {
    resolve: (value: unknown) => void
    reject: (reason?: unknown) => void
  },
}))

vi.mock('../../lib/api', () => {
  const get = vi.fn((path: string) => {
    if (path === '/api/workspaces') {
      if (state.defer) {
        return new Promise((resolve, reject) => {
          state.pending = { resolve, reject }
        })
      }
      if (state.error) return Promise.reject(state.error)
      return Promise.resolve(state.workspaces)
    }
    if (path === '/api/workspaces/archived') return Promise.resolve({ archived: [] })
    if (path === '/api/integrations/gws') {
      return Promise.resolve({ installed: false, binary_path: '', cli_available: false, profiles: [] })
    }
    return Promise.resolve({})
  })
  return {
    api: {
      get,
      post: vi.fn(() => Promise.resolve({})),
      patch: vi.fn(() => Promise.resolve({})),
      del: vi.fn(() => Promise.resolve({})),
    },
  }
})

vi.mock('../../lib/push', () => ({
  pushSupported: () => false,
  pushEnabled: () => false,
  enablePush: vi.fn(),
  disablePush: vi.fn(),
}))

const Stub = defineComponent({ render: () => h('div') })
const WORKSPACES = '/settings/workspaces'

function response(): WorkspacesResponse {
  return {
    workspaces: [
      {
        name: 'personal',
        vault_root: 'personal/memory-vault',
        default_provider: 'claude',
        gws_profile: '',
        color: 'pink',
        agent_fs_scope: 'workspace',
      },
      {
        name: 'work',
        vault_root: 'work/memory-vault',
        default_provider: 'claude',
        gws_profile: '',
        color: 'pink',
        agent_fs_scope: 'machine',
      },
    ],
    active: 'personal',
    primary: 'personal',
    default_agent_fs_scope: 'workspace',
    provider_options: [{ value: 'claude', label: 'Claude' }],
  }
}

function target(name: string): string {
  return `${WORKSPACES}?workspace=${name}#file-access`
}

async function settle(): Promise<void> {
  await flushPromises()
  await nextTick()
  await flushPromises()
  await nextTick()
}

async function mountSettings(path = WORKSPACES): Promise<{
  wrapper: VueWrapper
  router: Router
}> {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/settings', component: Stub }, { path: '/settings/:tab', component: Stub }],
  })
  await router.push(path)
  await router.isReady()
  const { default: SettingsView } = await import('../SettingsView.vue')
  const wrapper = mount(SettingsView, {
    attachTo: document.body,
    global: { plugins: [router], stubs: { Teleport: true, UpdateProgressView: Stub } },
  })
  return { wrapper, router }
}

function cardFor(wrapper: VueWrapper, name: string) {
  const card = wrapper.findAll('.workspace-list .workspace-card')
    .find(c => c.find('.workspace-title').text() === name)
  expect(card, `no card for ${name}`).toBeTruthy()
  return card!
}

function fsSelect(wrapper: VueWrapper, name: string) {
  return wrapper.find(`#workspace-file-access-${name}`)
}

function rect(top: number): DOMRect {
  return {
    top,
    bottom: top + 100,
    left: 0,
    right: 100,
    width: 100,
    height: 100,
    x: 0,
    y: top,
    toJSON: () => ({}),
  } as DOMRect
}

async function expectNoWrites(): Promise<void> {
  const { api } = await import('../../lib/api')
  expect(api.post).not.toHaveBeenCalled()
  expect(api.patch).not.toHaveBeenCalled()
  expect(api.del).not.toHaveBeenCalled()
}

let scrollTo: ReturnType<typeof vi.fn>
let windowScrollTo: ReturnType<typeof vi.fn>
let reduceMotion = false

beforeEach(() => {
  setActivePinia(createPinia())
  state.workspaces = response()
  state.error = null
  state.defer = false
  state.pending = null
  reduceMotion = false
  document.body.innerHTML = ''
  scrollTo = vi.fn()
  windowScrollTo = vi.fn()
  Object.defineProperty(HTMLElement.prototype, 'scrollTo', { configurable: true, value: scrollTo })
  Object.defineProperty(window, 'scrollTo', { configurable: true, value: windowScrollTo })
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: vi.fn((query: string) => ({
      matches: reduceMotion && query.includes('prefers-reduced-motion'),
      media: query,
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  })
})

afterEach(() => {
  document.body.innerHTML = ''
})

describe('Settings > Workspaces File access deep link', () => {
  it('opens the linked workspace, focuses File access and writes nothing', async () => {
    const { wrapper } = await mountSettings(target('work'))
    try {
      await settle()

      const card = cardFor(wrapper, 'work')
      expect(card.find('.set-row-body').exists()).toBe(true)
      expect(card.classes()).toContain('workspace-card--target')
      const select = fsSelect(wrapper, 'work')
      expect(select.exists()).toBe(true)
      expect((select.element as HTMLSelectElement).value).toBe('machine')
      expect(document.activeElement).toBe(select.element)

      expect(scrollTo).toHaveBeenCalledTimes(1)
      expect(vi.mocked(scrollTo).mock.contexts[0]).toBe(wrapper.find('.pane-body').element)
      expect(windowScrollTo).not.toHaveBeenCalled()
      const { useProjectStore } = await import('../../stores/projects')
      expect(useProjectStore().activeWorkspace).toBe('personal')
      await expectNoWrites()
    } finally {
      wrapper.unmount()
    }
  })

  it('scrolls the File access field row, not the card header, from the pane top', async () => {
    const { wrapper, router } = await mountSettings(target('work'))
    try {
      await settle()
      scrollTo.mockClear()

      const body = wrapper.find('.pane-body').element as HTMLElement
      const card = wrapper.find('#workspace-row-work').element as HTMLElement
      const fieldRow = wrapper.find('#workspace-file-access-row-work').element as HTMLElement
      // Distinct rects make the two candidates unambiguous: measuring the
      // header would scroll 500px short of the field row.
      Object.defineProperty(body, 'scrollTop', { configurable: true, value: 500 })
      body.getBoundingClientRect = () => rect(40)
      card.getBoundingClientRect = () => rect(200)
      fieldRow.getBoundingClientRect = () => rect(700)

      await router.push(WORKSPACES)
      await settle()
      await router.push(target('work'))
      await settle()

      // 500 (pane) + 700 - 40 (measured field row) - 16 (offset).
      expect(scrollTo).toHaveBeenCalledWith({ top: 1144, behavior: 'smooth' })
    } finally {
      wrapper.unmount()
    }
  })

  it('waits for a delayed workspace list before opening the row', async () => {
    state.defer = true
    const { wrapper, router } = await mountSettings()
    try {
      await settle()
      expect(wrapper.find('.workspace-list .workspace-card').exists()).toBe(false)

      await router.push(target('work'))
      await nextTick()
      expect(wrapper.find('.set-row-body').exists()).toBe(false)

      state.pending!.resolve(state.workspaces)
      state.pending = null
      await settle()

      expect(cardFor(wrapper, 'work').find('.set-row-body').exists()).toBe(true)
      expect(document.activeElement).toBe(fsSelect(wrapper, 'work').element)
      await expectNoWrites()
    } finally {
      wrapper.unmount()
    }
  })

  it('moves to a new target when the route changes', async () => {
    const { wrapper, router } = await mountSettings(target('personal'))
    try {
      await settle()
      expect(cardFor(wrapper, 'personal').find('.set-row-body').exists()).toBe(true)

      await router.push(target('work'))
      await settle()

      expect(cardFor(wrapper, 'personal').find('.set-row-body').exists()).toBe(false)
      expect(cardFor(wrapper, 'work').find('.set-row-body').exists()).toBe(true)
      expect(document.activeElement).toBe(fsSelect(wrapper, 'work').element)
    } finally {
      wrapper.unmount()
    }
  })

  it('does not let a superseded link steal focus after a later one', async () => {
    state.defer = true
    const { wrapper, router } = await mountSettings()
    try {
      await router.push(target('personal'))
      await nextTick()
      await router.push(target('work'))
      await nextTick()

      state.pending!.resolve(state.workspaces)
      state.pending = null
      await settle()

      expect(cardFor(wrapper, 'personal').find('.set-row-body').exists()).toBe(false)
      expect(cardFor(wrapper, 'work').find('.set-row-body').exists()).toBe(true)
      expect(document.activeElement).toBe(fsSelect(wrapper, 'work').element)
    } finally {
      wrapper.unmount()
    }
  })

  it('keeps the same editor draft when that workspace is linked again', async () => {
    const { wrapper, router } = await mountSettings(target('work'))
    try {
      await settle()
      const nameInput = cardFor(wrapper, 'work').find('.set-row-body input.routine-input')
      await nameInput.setValue('work-renamed')
      await fsSelect(wrapper, 'work').setValue('workspace')
      await nextTick()

      await router.push(WORKSPACES)
      await settle()
      await router.push(target('work'))
      await settle()

      expect(cardFor(wrapper, 'work').find('.set-row-body').exists()).toBe(true)
      expect((cardFor(wrapper, 'work').find('.set-row-body input.routine-input').element as HTMLInputElement).value)
        .toBe('work-renamed')
      expect((fsSelect(wrapper, 'work').element as HTMLSelectElement).value).toBe('workspace')
      await expectNoWrites()
    } finally {
      wrapper.unmount()
    }
  })

  it('refuses to open another workspace over a different unsaved name draft', async () => {
    const { wrapper, router } = await mountSettings()
    try {
      await settle()
      await cardFor(wrapper, 'personal').find('[aria-label="Edit workspace personal"]').trigger('click')
      await nextTick()
      await cardFor(wrapper, 'personal').find('.set-row-body input.routine-input').setValue('home')

      await router.push(target('work'))
      await settle()

      const personal = cardFor(wrapper, 'personal')
      expect(personal.find('.set-row-body').exists()).toBe(true)
      expect((personal.find('.set-row-body input.routine-input').element as HTMLInputElement).value).toBe('home')
      expect(cardFor(wrapper, 'work').find('.set-row-body').exists()).toBe(false)
      const status = wrapper.find('.workspace-link-status')
      expect(status.exists()).toBe(true)
      expect(status.text()).toContain('personal')
      expect(status.text()).toContain('work')
      expect(status.text()).toContain('Save or discard')
      expect(scrollTo).not.toHaveBeenCalled()
      await expectNoWrites()
    } finally {
      wrapper.unmount()
    }
  })

  it('also refuses over an unsaved File access change alone', async () => {
    const { wrapper, router } = await mountSettings()
    try {
      await settle()
      await cardFor(wrapper, 'personal').find('[aria-label="Edit workspace personal"]').trigger('click')
      await nextTick()
      await fsSelect(wrapper, 'personal').setValue('machine')
      await nextTick()

      await router.push(target('work'))
      await settle()

      expect(cardFor(wrapper, 'personal').find('.set-row-body').exists()).toBe(true)
      expect((fsSelect(wrapper, 'personal').element as HTMLSelectElement).value).toBe('machine')
      expect(cardFor(wrapper, 'work').find('.set-row-body').exists()).toBe(false)
      expect(wrapper.find('.workspace-link-status').text()).toContain('Save or discard')
      await expectNoWrites()
    } finally {
      wrapper.unmount()
    }
  })

  it('does nothing for a plain tab or a malformed link', async () => {
    const { wrapper, router } = await mountSettings()
    try {
      await settle()
      for (const path of [
        WORKSPACES,
        `${WORKSPACES}?workspace=work`,
        `${WORKSPACES}?workspace=work#other`,
        `${WORKSPACES}?workspace=work&action=save#file-access`,
        `${WORKSPACES}?workspace=work&workspace=personal#file-access`,
        `/settings/providers?workspace=work#file-access`,
      ]) {
        await router.push(path)
        await settle()
        expect(wrapper.findAll('.set-row-body'), path).toHaveLength(0)
        expect(wrapper.find('.workspace-link-status').exists(), path).toBe(false)
      }
      expect(scrollTo).not.toHaveBeenCalled()
      await expectNoWrites()
    } finally {
      wrapper.unmount()
    }
  })

  it('names a missing workspace and says nothing changed', async () => {
    const { wrapper, router } = await mountSettings(target('ghost'))
    try {
      await settle()

      expect(wrapper.findAll('.set-row-body')).toHaveLength(0)
      const status = wrapper.find('.workspace-link-status')
      expect(status.exists()).toBe(true)
      expect(status.text()).toContain('ghost')
      expect(status.text()).toContain('No settings changed')
      expect(scrollTo).not.toHaveBeenCalled()
      await expectNoWrites()

      await router.push(WORKSPACES)
      await settle()
      expect(wrapper.find('.workspace-link-status').exists()).toBe(false)
    } finally {
      wrapper.unmount()
    }
  })

  it('reports a load failure as an error rather than a missing workspace', async () => {
    state.error = new Error('boom')
    const { wrapper } = await mountSettings(target('work'))
    try {
      await settle()

      expect(wrapper.text()).toContain('Failed to load workspaces: boom')
      expect(wrapper.find('.workspace-link-status').exists()).toBe(false)
      expect(wrapper.findAll('.set-row-body')).toHaveLength(0)
      await expectNoWrites()
    } finally {
      wrapper.unmount()
    }
  })

  it('keeps the target reachable when motion is reduced', async () => {
    reduceMotion = true
    const { wrapper } = await mountSettings(target('work'))
    try {
      await settle()

      expect(document.activeElement).toBe(fsSelect(wrapper, 'work').element)
      expect(scrollTo).toHaveBeenCalledWith(expect.objectContaining({ behavior: 'auto' }))
      expect(cardFor(wrapper, 'work').classes()).toContain('workspace-card--target')
    } finally {
      wrapper.unmount()
    }
  })
})
