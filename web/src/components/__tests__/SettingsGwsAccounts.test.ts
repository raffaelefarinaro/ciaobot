// @vitest-environment jsdom

// The Google Workspace account cards in Settings > Workspaces. Two things are
// easy to regress here: the card header (title, status chip and "Remove
// account" must stay three separate siblings on one wrapping row — an earlier
// layout let the chip print on top of the wrapped title), and the recovery
// commands, which are noise once the account is connected and therefore sit
// behind a "Manual setup" disclosure in that state only.

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h, nextTick } from 'vue'
import type { GwsIntegrationProfile, GwsIntegrationSettings } from '../../lib/types'

const gwsState = vi.hoisted(() => ({ value: null as unknown }))

vi.mock('../../lib/api', () => {
  const get = vi.fn((path: string) => {
    if (path === '/api/integrations/gws') return Promise.resolve(gwsState.value)
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

function profile(overrides: Partial<GwsIntegrationProfile> = {}): GwsIntegrationProfile {
  return {
    name: 'personal',
    label: 'Personal Google account',
    purpose: 'Personal mail, calendar and drive.',
    examples: ['gmail', 'calendar'],
    configured: true,
    credentials_present: true,
    client_secret_present: true,
    config_dir: '/tmp/secrets/gws-personal',
    workspaces: ['personal'],
    setup_command: 'ciao gws personal auth login --full',
    headless_auth_command: 'ciao gws-auth-helper personal',
    email: 'someone@example.com',
    token_valid: true,
    token_error: '',
    needs_relogin: false,
    loopback_eligible: true,
    ...overrides,
  }
}

function integration(profiles: GwsIntegrationProfile[]): GwsIntegrationSettings {
  return {
    installed: true,
    binary_path: '/usr/local/bin/gws',
    default_profile: 'personal',
    cli_available: true,
    profiles,
  }
}

async function mountWorkspacesTab() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/settings', component: Stub }, { path: '/settings/:tab', component: Stub }],
  })
  await router.push('/settings/workspaces')
  await router.isReady()
  const { default: SettingsView } = await import('../SettingsView.vue')
  const wrapper = mount(SettingsView, {
    global: { plugins: [router], stubs: { Teleport: true, UpdateProgressView: Stub } },
  })
  await flushPromises()
  await nextTick()
  return wrapper
}

describe('the Google Workspace account card header', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    gwsState.value = integration([profile()])
  })

  it('keeps the title, the status chip and Remove account as separate siblings', async () => {
    const wrapper = await mountWorkspacesTab()
    const header = wrapper.find('.gws-profile-header')
    expect(header.exists()).toBe(true)

    const children = Array.from(header.element.children)
    // Exactly three boxes on the row, in reading order: no chip layered over
    // the heading, no wrapper reintroducing an overlap.
    expect(children).toHaveLength(3)
    expect(children[0].classList.contains('gws-profile-heading')).toBe(true)
    expect(children[1].classList.contains('gws-profile-badge')).toBe(true)
    expect(children[2].classList.contains('gws-profile-remove')).toBe(true)

    expect(header.find('.gws-profile-title').text()).toBe('Personal Google account')
    expect(header.find('.gws-profile-badge').text()).toBe('Authenticated')
    expect(header.find('button.gws-profile-remove').text()).toBe('Remove account')
    // The title must not be nested inside the chip or the button.
    expect(children[1].querySelector('.gws-profile-title')).toBeNull()
    expect(children[2].querySelector('.gws-profile-title')).toBeNull()
    wrapper.unmount()
  })

  it('pins the action row to the bottom of every card', async () => {
    gwsState.value = integration([
      profile(),
      profile({ name: 'work', label: 'Work Google account', workspaces: [], examples: [] }),
    ])
    const wrapper = await mountWorkspacesTab()
    const cards = wrapper.findAll('.gws-profile-card')
    expect(cards).toHaveLength(2)
    for (const card of cards) {
      const last = card.element.lastElementChild
      expect(last?.classList.contains('gws-profile-actions')).toBe(true)
    }
    wrapper.unmount()
  })
})

describe('the manual setup disclosure', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('hides the recovery commands behind a toggle once authenticated', async () => {
    gwsState.value = integration([profile()])
    const wrapper = await mountWorkspacesTab()

    expect(wrapper.find('#gws-manual-personal').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('ciao gws personal auth login --full')
    expect(wrapper.text()).not.toContain('ciao gws-auth-helper personal')

    const toggle = wrapper.find('button.gws-manual-toggle')
    expect(toggle.exists()).toBe(true)
    expect(toggle.attributes('aria-expanded')).toBe('false')
    expect(toggle.attributes('aria-controls')).toBe('gws-manual-personal')

    await toggle.trigger('click')
    await nextTick()

    expect(toggle.attributes('aria-expanded')).toBe('true')
    const panel = wrapper.find('#gws-manual-personal')
    expect(panel.exists()).toBe(true)
    // Still plain, selectable command text.
    const commands = panel.findAll('code.gws-command').map(c => c.text())
    expect(commands).toEqual([
      'ciao gws personal auth login --full',
      'ciao gws-auth-helper personal',
    ])

    await toggle.trigger('click')
    await nextTick()
    expect(toggle.attributes('aria-expanded')).toBe('false')
    expect(wrapper.find('#gws-manual-personal').exists()).toBe(false)
    wrapper.unmount()
  })

  it('shows the recovery commands outright while the account is not connected', async () => {
    gwsState.value = integration([profile({ configured: false })])
    const wrapper = await mountWorkspacesTab()

    expect(wrapper.find('button.gws-manual-toggle').exists()).toBe(false)
    const panel = wrapper.find('#gws-manual-personal')
    expect(panel.exists()).toBe(true)
    expect(panel.text()).toContain('ciao gws personal auth login --full')
    expect(panel.text()).toContain('ciao gws-auth-helper personal')
    wrapper.unmount()
  })
})
