// @vitest-environment jsdom

// Settings > Workspace > Overview: the "Claude context trimming" card. It edits
// the workspace draft's claude_trim and disallowed_tools; the Chats and models
// Save changes row sends them, and nothing is sent by the card itself.

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h, nextTick } from 'vue'
import type { ClaudeTrimKind, ClaudeTrimOption, WorkspacesResponse } from '../../lib/types'

const state = vi.hoisted(() => ({
  workspaces: null as unknown,
}))

vi.mock('../../lib/api', () => {
  const get = vi.fn((path: string) => {
    if (path === '/api/workspaces') return Promise.resolve(state.workspaces)
    if (path === '/api/workspaces/archived') return Promise.resolve({ archived: [] })
    if (path.startsWith('/api/proposals/handover')) {
      return Promise.resolve({
        workspace: 'personal',
        handover: 'off',
        acting_available: true,
        agreement: {
          decided: 0, matched: 0, rate: null, window: 50, min_decisions: 20, threshold: 0.9,
          predicted_accept: 0, accept_matched: 0, predicted_dismiss: 0, dismiss_matched: 0,
          suggest_handover: false,
        },
      })
    }
    if (path.startsWith('/api/update-tasks')) return Promise.resolve({ tasks: [] })
    if (path === '/api/integrations/gws') {
      return Promise.resolve({
        installed: false,
        binary_path: '',
        cli_available: false,
        profiles: [],
      })
    }
    // The Overview's Chats and models card reads the engine defaults.
    if (path === '/api/settings/routines') return Promise.resolve({ model_options: { anthropic: [] } })
    return Promise.resolve({})
  })
  return {
    api: {
      get,
      post: vi.fn(() => Promise.resolve(state.workspaces)),
      patch: vi.fn(() => Promise.resolve(state.workspaces)),
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

const opt = (o: Partial<ClaudeTrimOption> & { key: string }): ClaudeTrimOption => ({
  kind: o.key.split(':')[0] as ClaudeTrimKind,
  name: o.key.split(':')[1],
  label: o.key,
  reason: `${o.key} reason`,
  group: 'features',
  default_trimmed: false,
  locked: false,
  ...o,
})

// The engine's catalog shape: a locked scheduling option, two features, a review
// option, four bundled skills (two trimmed by default) and a setting.
const CATALOG: ClaudeTrimOption[] = [
  opt({ key: 'tool:CronCreate', label: 'Cron schedules', reason: 'Schedules runs.', group: 'scheduling', default_trimmed: true, locked: true }),
  opt({ key: 'tool:NotebookEdit', label: 'Notebook editing', reason: 'Edits Jupyter notebooks.', default_trimmed: true }),
  opt({ key: 'tool:ReportFindings', label: 'Report findings', reason: 'Reports review findings.', group: 'review' }),
  opt({ key: 'skill:a', label: 'Skill A', group: 'skills', default_trimmed: true }),
  opt({ key: 'skill:b', label: 'Skill B', group: 'skills', default_trimmed: true }),
  opt({ key: 'skill:c', label: 'Skill C', group: 'skills' }),
  opt({ key: 'skill:d', label: 'Skill D', group: 'skills' }),
  opt({ key: 'setting:disableWorkflows', kind: 'setting', label: 'Workflows', reason: 'Turns off workflows.' }),
]

function workspaces(entryExtra: Record<string, unknown> = {}, withCatalog = true): WorkspacesResponse {
  const entry: Record<string, unknown> = {
    name: 'personal',
    vault_root: 'personal/memory-vault',
    default_provider: 'claude',
    gws_profile: '',
    color: 'pink',
    agent_fs_scope: null,
    ...entryExtra,
  }
  return {
    workspaces: [entry],
    active: 'personal',
    primary: 'personal',
    provider_options: [{ value: 'claude', label: 'Claude' }],
    default_agent_fs_scope: 'workspace',
    ...(withCatalog ? { claude_trim_options: CATALOG } : {}),
  } as unknown as WorkspacesResponse
}

async function mountWorkspacesTab(path = '/settings/workspace') {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/settings', component: Stub }, { path: '/settings/:tab', component: Stub }],
  })
  await router.push(path)
  await router.isReady()
  const { default: SettingsView } = await import('../SettingsView.vue')
  const wrapper = mount(SettingsView, {
    global: { plugins: [router], stubs: { Teleport: true, UpdateProgressView: Stub } },
  })
  await flushPromises()
  await nextTick()
  return wrapper
}

type Mounted = Awaited<ReturnType<typeof mountWorkspacesTab>>

// Rows are found by their label text, so the switch is reached the way a user names it.
function trimSwitch(wrapper: Mounted, label: string) {
  const row = wrapper.findAll('.trim-row').find(r => r.find('.set-subrow-label').text() === label)
  expect(row, `no trim row "${label}"`).toBeTruthy()
  return row!.get('button[role="switch"]')
}

describe('Settings > Workspaces Claude context trimming card', () => {
  beforeEach(async () => {
    setActivePinia(createPinia())
    const { api } = await import('../../lib/api')
    vi.mocked(api.patch).mockClear()
  })

  it('renders the catalog grouped, with the scope note', async () => {
    state.workspaces = workspaces()
    const wrapper = await mountWorkspacesTab()
    try {
      const card = wrapper.get('.trim-card')
      expect(card.get('.section-title').text()).toBe('Claude context trimming')
      expect(wrapper.findAll('.trim-group')).toHaveLength(4)
      const titles = wrapper.findAll('.trim-group-title').map(t => t.text())
      expect(titles).toEqual(expect.arrayContaining(['Scheduling', 'Features', 'Review']))
      const disclosures = wrapper.findAll('details.trim-disclosure')
      expect(disclosures).toHaveLength(1)
      expect(disclosures[0].get('summary').text()).toBe('Bundled skills · 2 of 4 hidden')
      expect(card.text()).toContain('Claude chats only')
      expect(card.text()).toContain('next turn')
      expect(trimSwitch(wrapper, 'Notebook editing').attributes('aria-checked')).toBe('false')
      expect(trimSwitch(wrapper, 'Notebook editing').text()).toBe('Off')
      expect(trimSwitch(wrapper, 'Report findings').attributes('aria-checked')).toBe('true')
      expect(trimSwitch(wrapper, 'Report findings').text()).toBe('On')
    } finally {
      wrapper.unmount()
    }
  })

  it('hides the card when the engine sends no catalog', async () => {
    state.workspaces = workspaces({}, false)
    const wrapper = await mountWorkspacesTab()
    try {
      expect(wrapper.find('.trim-card').exists()).toBe(false)
    } finally {
      wrapper.unmount()
    }
  })

  it('lists the card in the On this page rail', async () => {
    state.workspaces = workspaces()
    const wrapper = await mountWorkspacesTab()
    try {
      const labels = wrapper.findAll('.settings-toc-item').map(item => item.text())
      const at = labels.indexOf('Claude context trimming')
      expect(at).toBeGreaterThan(-1)
      expect(labels[at - 1]).toBe('Workspace')
      expect(labels[at + 1]).toBe('Chats and models')
    } finally {
      wrapper.unmount()
    }
  })

  it('locked options are disabled and say why', async () => {
    state.workspaces = workspaces()
    const wrapper = await mountWorkspacesTab()
    try {
      const toggle = trimSwitch(wrapper, 'Cron schedules')
      expect(toggle.attributes('disabled')).toBeDefined()
      expect(toggle.attributes('aria-checked')).toBe('false')
      expect(wrapper.get('.trim-card').text()).toContain('Always hidden — Schedules runs.')
      await toggle.trigger('click')
      await flushPromises()
      expect(trimSwitch(wrapper, 'Cron schedules').attributes('aria-checked')).toBe('false')
      expect(wrapper.find('.workspace-save').exists()).toBe(false)
    } finally {
      wrapper.unmount()
    }
  })

  it('toggling saves a sparse claude_trim', async () => {
    const { api } = await import('../../lib/api')
    state.workspaces = workspaces()
    const wrapper = await mountWorkspacesTab()
    try {
      await trimSwitch(wrapper, 'Notebook editing').trigger('click')
      await trimSwitch(wrapper, 'Report findings').trigger('click')
      await flushPromises()
      expect(trimSwitch(wrapper, 'Notebook editing').attributes('aria-checked')).toBe('true')
      expect(trimSwitch(wrapper, 'Report findings').attributes('aria-checked')).toBe('false')

      await wrapper.get('.workspace-save').trigger('click')
      await flushPromises()

      expect(api.patch).toHaveBeenCalledWith(
        '/api/workspaces/personal',
        expect.objectContaining({ claude_trim: { 'tool:NotebookEdit': false, 'tool:ReportFindings': true } }),
      )
    } finally {
      wrapper.unmount()
    }
  })

  it('toggling back to the default leaves the form clean', async () => {
    state.workspaces = workspaces()
    const wrapper = await mountWorkspacesTab()
    try {
      await trimSwitch(wrapper, 'Notebook editing').trigger('click')
      await trimSwitch(wrapper, 'Notebook editing').trigger('click')
      await flushPromises()
      expect(wrapper.find('.workspace-save').exists()).toBe(false)
    } finally {
      wrapper.unmount()
    }
  })

  it('Reset to defaults sends {}', async () => {
    const { api } = await import('../../lib/api')
    state.workspaces = workspaces({ claude_trim: { 'tool:NotebookEdit': false } })
    const wrapper = await mountWorkspacesTab()
    try {
      expect(trimSwitch(wrapper, 'Notebook editing').attributes('aria-checked')).toBe('true')
      expect(wrapper.get('.trim-reset').attributes('disabled')).toBeUndefined()
      await wrapper.get('.trim-reset').trigger('click')
      await flushPromises()
      expect(trimSwitch(wrapper, 'Notebook editing').attributes('aria-checked')).toBe('false')

      await wrapper.get('.workspace-save').trigger('click')
      await flushPromises()
      expect(api.patch).toHaveBeenCalledWith(
        '/api/workspaces/personal',
        expect.objectContaining({ claude_trim: {} }),
      )
    } finally {
      wrapper.unmount()
    }
  })

  it('Reset to defaults is disabled when nothing is overridden', async () => {
    state.workspaces = workspaces()
    const wrapper = await mountWorkspacesTab()
    try {
      expect(wrapper.get('.trim-reset').attributes('disabled')).toBeDefined()
    } finally {
      wrapper.unmount()
    }
  })

  it('extra denied tools shows and saves disallowed_tools', async () => {
    const { api } = await import('../../lib/api')
    state.workspaces = workspaces({ disallowed_tools: ['mcp__x'] })
    const wrapper = await mountWorkspacesTab()
    try {
      const field = wrapper.get('#trim-extra-tools-personal')
      expect((field.element as HTMLInputElement).value).toBe('mcp__x')
      await field.setValue('mcp__slack, Bash')
      await wrapper.get('.workspace-save').trigger('click')
      await flushPromises()

      expect(api.patch).toHaveBeenCalledWith(
        '/api/workspaces/personal',
        expect.objectContaining({ disallowed_tools: ['mcp__slack', 'Bash'] }),
      )
      expect(vi.mocked(api.patch).mock.calls[0][1]).not.toHaveProperty('claude_trim')
    } finally {
      wrapper.unmount()
    }
  })
})
