// @vitest-environment jsdom

// The Google Workspace account cards in Settings > Workspaces. Two things are
// easy to regress here: the card header (title, status chip and "Remove
// account" must stay three separate siblings on one wrapping row — an earlier
// layout let the chip print on top of the wrapped title), and the recovery
// commands, which sit in a closed "Advanced / headless setup" disclosure on every
// card.

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

describe('the advanced setup disclosure', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('keeps the recovery commands in a closed details once authenticated', async () => {
    gwsState.value = integration([profile()])
    const wrapper = await mountWorkspacesTab()

    const details = wrapper.find('details.gws-advanced')
    expect(details.exists()).toBe(true)
    expect(details.attributes('open')).toBeUndefined()
    expect(details.find('summary').text()).toBe('Advanced / headless setup')
    const commands = wrapper.find('#gws-manual-personal').findAll('code.gws-command').map(c => c.text())
    expect(commands).toEqual([
      'ciao gws personal auth login --full',
      'ciao gws-auth-helper personal',
    ])
    wrapper.unmount()
  })

  it('keeps the recovery commands closed while the account is not connected', async () => {
    gwsState.value = integration([profile({ configured: false })])
    const wrapper = await mountWorkspacesTab()

    const details = wrapper.find('details.gws-advanced')
    expect(details.exists()).toBe(true)
    expect(details.attributes('open')).toBeUndefined()
    expect(wrapper.find('#gws-manual-personal').text()).toContain('ciao gws personal auth login --full')
    expect(wrapper.find('#gws-manual-personal').text()).toContain('ciao gws-auth-helper personal')
    wrapper.unmount()
  })

  it('offers the gcloud client command only while no client exists', async () => {
    gwsState.value = integration([
      profile({ client_secret_present: false, configured: false }),
      profile({ name: 'work', label: 'Work Google account', workspaces: ['work'] }),
    ])
    const wrapper = await mountWorkspacesTab()

    const [needsClient, ready] = wrapper.findAll('details.gws-advanced')
    expect(needsClient.findAll('code.gws-command').map(c => c.text())).toContain('ciao gws personal auth setup')
    expect(ready.findAll('code.gws-command').map(c => c.text())).not.toContain('ciao gws work auth setup')
    wrapper.unmount()
  })
})

describe('the sign-in button before a client exists', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('shows a disabled Sign in with Google with its reason', async () => {
    gwsState.value = integration([profile({ client_secret_present: false, configured: false })])
    const wrapper = await mountWorkspacesTab()

    const button = wrapper.findAll('button').find(b => b.text() === 'Sign in with Google')
    expect(button).toBeDefined()
    expect(button!.attributes('disabled')).toBeDefined()

    const describedBy = button!.attributes('aria-describedby')
    expect(describedBy).toBeTruthy()
    expect(wrapper.find(`#${describedBy}`).text()).toBe('Upload an OAuth client first.')

    expect(wrapper.find('.gws-profile-actions').text()).not.toContain('auth setup')
    wrapper.unmount()
  })
})

describe('the setup checklist', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('marks the current step on the section', async () => {
    gwsState.value = integration([profile({ client_secret_present: false, configured: false })])
    const wrapper = await mountWorkspacesTab()

    const current = wrapper.find('.gws-setup-step[aria-current="step"]')
    expect(current.exists()).toBe(true)
    expect(current.text()).toContain('Add an OAuth client')
    wrapper.unmount()
  })

  it('hides once every account is set up', async () => {
    gwsState.value = integration([profile()])
    const wrapper = await mountWorkspacesTab()

    expect(wrapper.find('.gws-setup').exists()).toBe(false)
    wrapper.unmount()
  })
})
