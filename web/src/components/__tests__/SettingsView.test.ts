// @vitest-environment jsdom

// Settings → Workspaces → Vault location.
//
// Two regressions are pinned here.
//
// 1. The picker dialog was announced as a dialog but behaved like ordinary page
//    content: no `aria-modal`, no Escape, no initial focus and no Tab trap, so
//    a keyboard user's next Tab left the overlay and landed on the Settings
//    page behind it with no way back and no way out.
// 2. `workspaceVaultDisplay` fell back to the workspace NAME, which rendered
//    `personal` inside a `<code>` where a filesystem path belongs — and the
//    same value decided where the picker opened, so a bare name or a relative
//    registry root such as `memory-vault/personal` sent it to `~` instead of
//    the vault about to be moved.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { config, flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h, nextTick } from 'vue'
import { api } from '../../lib/api'

const WORKSPACE_ROOT = '/srv/ciao'
const SHARED_VAULT_ROOT = '/srv/ciao/memory-vault'

vi.mock('../../lib/api', () => {
  const routineSettings = {
    insights_model: '',
    critique_models: '',
    insights_model_effective: 'haiku',
    critique_models_effective: 'anthropic/claude-haiku-4.5',
    transcription: { locale: 'en-US', available: false, unavailable_reason: 'needs macOS 26' },
    speech: { local_voice: '', available: false, local_voices: [] },
    model_options: { anthropic: ['haiku', 'sonnet', 'opus'] },
    backends: { anthropic: true },
    workspace_context: {
      workspace_root: '/srv/ciao',
      vault_root: '/srv/ciao/memory-vault',
    },
  }
  // Mirrors GET /api/workspaces: `vault_root` is the registry value verbatim,
  // which is a relative path in the standard layout and absolute only for a
  // pinned/external root.
  const workspaces = {
    workspaces: [
      {
        name: 'personal',
        vault_root: 'memory-vault/personal',
        default_provider: 'claude',
        default_model: '',
        gws_profile: '',
        disallowed_tools: null,
        color: 'pink',
        vault_pinned: false,
      },
      {
        name: 'client-a',
        vault_root: '/Volumes/work/client-a',
        default_provider: 'claude',
        default_model: '',
        gws_profile: '',
        disallowed_tools: null,
        color: 'cyan',
        vault_pinned: true,
      },
    ],
    active: 'personal',
    app_default_model: 'sonnet',
    provider_options: [{ value: 'claude', label: 'Anthropic (via Claude Code)' }],
  }
  const listing = {
    path: '/srv/ciao/memory-vault/personal',
    display_path: '~/ciao/memory-vault/personal',
    parent: '/srv/ciao/memory-vault',
    dirs: [{ name: 'projects', path: '/srv/ciao/memory-vault/personal/projects' }],
    home: '/Users/alice',
  }
  const responses: Record<string, unknown> = {
    '/api/settings': {},
    '/api/settings/providers': { keys: {}, service_keys: {}, connections: {}, env_path: '' },
    '/api/settings/routines': routineSettings,
    '/api/local/status': { git_repo: true, branch: 'main', dirty: false },
    '/api/workspaces': workspaces,
    '/api/workspaces/browse-folder': listing,
    '/api/models': {
      models: ['haiku', 'sonnet', 'opus'],
      default: 'sonnet',
      provider_models: { claude: ['haiku', 'sonnet', 'opus'] },
      provider_defaults: { claude: 'sonnet' },
    },
    '/api/projects': [],
    '/api/chats': [],
    '/api/schedules': [],
    '/api/automation': [],
  }
  const get = vi.fn((rawPath: string) => {
    const path = rawPath.split('?')[0]
    if (path in responses) return Promise.resolve(responses[path])
    return Promise.resolve([])
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
  isPushEnabled: () => Promise.resolve(false),
  enablePush: vi.fn(),
  disablePush: vi.fn(),
  currentSubscription: () => Promise.resolve(null),
}))

const NoopStub = { name: 'NoopStub', render: () => h('div') }
vi.mock('../VoiceRecorder.vue', () => ({ default: NoopStub }))
vi.mock('../InAppToast.vue', () => ({ default: NoopStub }))

const Stub = defineComponent({ render: () => h('div') })

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: Stub },
      { path: '/settings', name: 'settings', component: Stub },
      { path: '/settings/:tab', name: 'settings-tab', component: Stub },
    ],
  })
}

beforeEach(() => {
  setActivePinia(createPinia())
  config.global.stubs = { Teleport: true }
})

afterEach(() => {
  vi.clearAllMocks()
  document.body.innerHTML = ''
})

async function mountWorkspacesTab() {
  const router = makeRouter()
  await router.push('/settings/workspaces')
  await router.isReady()
  const mod = await import('../SettingsView.vue')
  const wrapper = mount(mod.default as never, {
    attachTo: document.body,
    global: { plugins: [router] },
  })
  await flushPromises()
  await nextTick()
  await flushPromises()
  return wrapper
}

function vaultRow(wrapper: Awaited<ReturnType<typeof mountWorkspacesTab>>, index: number) {
  return wrapper.findAll('.vault-location-path')[index]
}

/** The "Move…" button belonging to one workspace card, focused as a real click
 * would leave it so the dialog's focus restore has a trigger to return to. */
function moveTrigger(wrapper: Awaited<ReturnType<typeof mountWorkspacesTab>>, index: number) {
  const button = wrapper
    .findAll('.vault-location-row button')
    .filter((el) => el.text().startsWith('Move'))[index]
  ;(button.element as HTMLButtonElement).focus()
  return button
}

async function openPicker(wrapper: Awaited<ReturnType<typeof mountWorkspacesTab>>, index = 0) {
  await moveTrigger(wrapper, index).trigger('click')
  await flushPromises()
  await nextTick()
  await flushPromises()
}

function focusables(wrapper: Awaited<ReturnType<typeof mountWorkspacesTab>>): HTMLElement[] {
  const modal = wrapper.find('.picker-modal').element
  const selector = 'a[href], button, input, select, textarea, [tabindex]:not([tabindex="-1"])'
  return Array.from(modal.querySelectorAll<HTMLElement>(selector)).filter(
    (el) => !el.hasAttribute('disabled'),
  )
}

describe('Settings → Workspaces vault location', () => {
  it('renders the resolved vault path, never the bare workspace name', async () => {
    const wrapper = await mountWorkspacesTab()

    // Relative registry root, resolved against the workspace root exactly as
    // CiaoConfig._resolve_vault_root does.
    expect(vaultRow(wrapper, 0).text()).toBe(`${WORKSPACE_ROOT}/memory-vault/personal`)
    expect(vaultRow(wrapper, 0).text()).not.toBe('personal')
    // An absolute pinned root is shown as stored.
    expect(vaultRow(wrapper, 1).text()).toBe('/Volumes/work/client-a')
    // Whatever is shown must look like a filesystem path.
    for (const row of wrapper.findAll('.vault-location-path')) {
      expect(row.text().startsWith('/')).toBe(true)
    }
    expect(SHARED_VAULT_ROOT).toBe(`${WORKSPACE_ROOT}/memory-vault`)

    wrapper.unmount()
  })

  it('opens the picker at the vault it is about to move, not at ~', async () => {
    const wrapper = await mountWorkspacesTab()
    await openPicker(wrapper, 0)

    const browseCalls = vi
      .mocked(api.get)
      .mock.calls.map((call) => String(call[0]))
      .filter((path) => path.startsWith('/api/workspaces/browse-folder'))
    expect(browseCalls).toHaveLength(1)
    expect(browseCalls[0]).toBe(
      `/api/workspaces/browse-folder?path=${encodeURIComponent(`${WORKSPACE_ROOT}/memory-vault/personal`)}`,
    )

    wrapper.unmount()
  })

  it('marks the picker as a modal dialog and gives it focus on open', async () => {
    const wrapper = await mountWorkspacesTab()
    await openPicker(wrapper)

    const modal = wrapper.find('.picker-modal')
    expect(modal.attributes('role')).toBe('dialog')
    expect(modal.attributes('aria-modal')).toBe('true')
    expect(modal.attributes('tabindex')).toBe('-1')
    // Focus is inside the dialog, so the first Tab cannot reach the page behind.
    expect(modal.element.contains(document.activeElement)).toBe(true)

    wrapper.unmount()
  })

  it('closes on Escape and returns focus to the button that opened it', async () => {
    const wrapper = await mountWorkspacesTab()
    const trigger = moveTrigger(wrapper, 0).element as HTMLButtonElement
    await openPicker(wrapper)
    expect(wrapper.find('.picker-modal').exists()).toBe(true)

    await wrapper.find('.picker-modal').trigger('keydown', { key: 'Escape' })
    await nextTick()
    await nextTick()

    expect(wrapper.find('.picker-modal').exists()).toBe(false)
    expect(document.activeElement).toBe(trigger)

    wrapper.unmount()
  })

  it('traps Tab inside the dialog in both directions', async () => {
    const wrapper = await mountWorkspacesTab()
    await openPicker(wrapper)

    const items = focusables(wrapper)
    expect(items.length).toBeGreaterThan(2)
    const first = items[0]
    const last = items[items.length - 1]

    last.focus()
    expect(document.activeElement).toBe(last)
    const forward = new KeyboardEvent('keydown', { key: 'Tab', bubbles: true, cancelable: true })
    last.dispatchEvent(forward)
    expect(forward.defaultPrevented).toBe(true)
    expect(document.activeElement).toBe(first)

    const backward = new KeyboardEvent('keydown', {
      key: 'Tab',
      shiftKey: true,
      bubbles: true,
      cancelable: true,
    })
    first.dispatchEvent(backward)
    expect(backward.defaultPrevented).toBe(true)
    expect(document.activeElement).toBe(last)

    wrapper.unmount()
  })
})
