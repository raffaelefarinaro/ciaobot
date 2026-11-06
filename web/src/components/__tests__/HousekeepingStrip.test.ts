// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { defineComponent, nextTick } from 'vue'
import HousekeepingStrip from '../HousekeepingStrip.vue'
import HomeNoticeReopen from '../HomeNoticeReopen.vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import { resetHomeNoticeWindows, useHomeNoticeWindows } from '../../composables/useHomeNoticeWindows'
import { api } from '../../lib/api'
import * as confirmModule from '../../lib/confirm'
import { actionNoticeKey, updateNoticeKey } from '../../lib/notificationInbox'
import { resetHostTerminalForTests, useHostTerminal } from '../../composables/useHostTerminal'
import { leadLabels, leads, menuLabels, pick, rows, titles, visibleControls } from './needsYouHelpers'
import { readyHealth } from '../../lib/sourceHealth'
import { useProjectStore } from '../../stores/projects'
import { useHousekeepingStore } from '../../stores/housekeeping'
import type { OperatorAction, UpdateTaskRow, WorkspaceHealthCheck } from '../../lib/types'

// The router is imported lazily inside the click handlers, so it is stubbed as a
// module: the component's own import is what has to be intercepted.
const routerPush = vi.hoisted(() => vi.fn(async () => {}))
vi.mock('../../router', () => ({ router: { push: routerPush, replace: vi.fn(async () => {}) } }))

// The reveal request is module-level state shared by every mounted strip, so a
// strip left mounted by an earlier test would answer a later test's request.
enableAutoUnmount(afterEach)

function action(overrides: Partial<OperatorAction> = {}): OperatorAction {
  return {
    id: 'test-action',
    kind: 'test',
    severity: 10,
    title: 'A condition needs you',
    detail: 'It can be fixed.',
    glyph: '▲',
    workspace: 'personal',
    view_label: '',
    blocking: false,
    view_route: '',
    run_label: '',
    chat_label: '',
    chat_prompt: '',
    ...overrides,
  }
}

function healthCheck(overrides: Partial<WorkspaceHealthCheck> = {}): WorkspaceHealthCheck {
  return {
    id: 'vault-root-work',
    title: 'Vault root for work is missing',
    status: 'error',
    detail: 'The folder does not exist.',
    path: '/vaults/work',
    action: 'Create it.',
    ...overrides,
  }
}

describe('HousekeepingStrip', () => {
  let pinia: ReturnType<typeof createPinia>

  beforeEach(() => {
    resetHomeNoticeWindows()
    pinia = createPinia()
    setActivePinia(pinia)
    const housekeeping = useHousekeepingStore()
    // Avoid the interval / focus listeners firing during tests.
    vi.spyOn(housekeeping, 'init').mockImplementation(() => {})
  })

  afterEach(() => {
    document.body.innerHTML = ''
    vi.restoreAllMocks()
  })

  it('renders nothing at all with zero rows (no wrapper element)', () => {
    const store = useHousekeepingStore()
    store.actions = []
    const wrapper = mount(HousekeepingStrip, { global: { plugins: [pinia] } })
    // No wrapper element, no heading, no "all clear" row: with nothing to show
    // the page below does not move, because there is nothing above it.
    expect(wrapper.find('.needs-you').exists()).toBe(false)
    expect(wrapper.find('section').exists()).toBe(false)
    expect(wrapper.findAll('*').length).toBe(0)
    wrapper.unmount()
  })

  it('is one section titled Needs you holding one row per action', async () => {
    const store = useHousekeepingStore()
    store.actions = [
      action({ id: 'a', run_label: 'Install' }),
      action({ id: 'b', chat_label: 'Fix in chat', chat_prompt: 'do it' }),
      action({ id: 'c', run_label: 'Run', chat_prompt: 'talk' }),
    ]
    const wrapper = mount(HousekeepingStrip, { global: { plugins: [pinia] } })
    await nextTick()
    expect(wrapper.findAll('section')).toHaveLength(1)
    expect(wrapper.get('#needs-you-heading').text()).toBe('Needs you')
    expect(wrapper.get('section').attributes('aria-labelledby')).toBe('needs-you-heading')
    expect(rows(wrapper)).toHaveLength(3)
    wrapper.unmount()
  })

  it('gives every row one title, one line and at most one lead control', async () => {
    const store = useHousekeepingStore()
    store.actions = [
      action({ id: 'a', run_label: 'Install', chat_prompt: 'x', dismiss_label: 'Later', link_label: 'Notes', link_url: 'https://example.test' }),
      action({ id: 'b', chat_label: 'Fix in chat', chat_prompt: 'do it' }),
      action({ id: 'c', view_label: 'Open queue', view_route: '/proposals', run_label: 'Run', dismiss_label: 'Not now' }),
      action({ id: 'd', dismiss_label: 'Later' }),
    ]
    const wrapper = mount(HousekeepingStrip, { global: { plugins: [pinia] } })
    await nextTick()
    for (const row of rows(wrapper)) {
      expect(row.findAll('.needs-you-title')).toHaveLength(1)
      expect(row.findAll('.needs-you-line')).toHaveLength(1)
      expect(leads(row)).toHaveLength(1)
      // Closed menu: the lead and the ⋯ trigger are the only controls.
      expect(visibleControls(row)).toHaveLength(2)
    }
    wrapper.unmount()
  })

  it('leads with run, and moves chat, link and dismiss into the menu', async () => {
    const store = useHousekeepingStore()
    store.actions = [action({
      id: 'a', run_label: 'Install', chat_label: 'Ask', chat_prompt: 'x',
      link_label: 'Notes', link_url: 'https://example.test/n', dismiss_label: 'Later',
    })]
    const wrapper = mount(HousekeepingStrip, { global: { plugins: [pinia] } })
    await nextTick()
    const [row] = rows(wrapper)
    expect(leadLabels(row)).toEqual(['Install'])
    expect(await menuLabels(row)).toEqual(['Notes', 'Ask', 'Later', 'Hide for now'])
    wrapper.unmount()
  })

  it('leads a chat-only action with its chat button', async () => {
    useHousekeepingStore().actions = [action({ chat_label: 'Fix in chat', chat_prompt: 'do it' })]
    const wrapper = mount(HousekeepingStrip, { global: { plugins: [pinia] } })
    await nextTick()
    const [row] = rows(wrapper)
    expect(leadLabels(row)).toEqual(['Fix in chat'])
    expect(await menuLabels(row)).toEqual(['Hide for now'])
    wrapper.unmount()
  })

  it('reduces a markdown detail to its first plain line', async () => {
    useHousekeepingStore().actions = [action({
      detail: 'The server was down past **2** reminders.\n\nMissed:\n- [Pay rent](/schedules/s1) — due today',
    })]
    const wrapper = mount(HousekeepingStrip, { global: { plugins: [pinia] } })
    await nextTick()
    expect(rows(wrapper)[0].get('.needs-you-line').text()).toBe('The server was down past 2 reminders.')
    wrapper.unmount()
  })

  it('hides a row for now without dismissing its action, and Show N closed notices brings it back', async () => {
    const store = useHousekeepingStore()
    store.actions = [action({ id: 'a' }), action({ id: 'b', title: 'Another condition' })]
    const dismiss = vi.spyOn(store, 'dismiss')
    const wrapper = mount(HousekeepingStrip, { global: { plugins: [pinia] }, attachTo: document.body })
    const reopen = mount(HomeNoticeReopen, { attachTo: document.body })
    await nextTick()
    await pick(rows(wrapper)[0], 'Hide for now')
    await flushPromises()
    expect(titles(wrapper)).toEqual(['Another condition'])
    expect(store.actions).toHaveLength(2)
    expect(dismiss).not.toHaveBeenCalled()
    expect(reopen.text()).toBe('Show 1 closed notice')
    // Focus moves to the recovery control, not the page start.
    expect(document.activeElement).toBe(reopen.get('button').element)
    await reopen.get('button').trigger('click')
    await nextTick()
    expect(rows(wrapper)).toHaveLength(2)
    reopen.unmount()
    wrapper.unmount()
  })

  it('run button calls run and the list collapses when the response empties it', async () => {
    const store = useHousekeepingStore()
    store.actions = [action({ id: 'a', run_label: 'Run' })]
    const runSpy = vi.spyOn(store, 'run').mockResolvedValue({ ok: true, summary: '' })
    const wrapper = mount(HousekeepingStrip, { global: { plugins: [pinia] } })
    await nextTick()
    const runButton = leads(rows(wrapper)[0])[0]
    expect(runButton.text()).toBe('Run')
    await runButton.trigger('click')
    expect(runSpy).toHaveBeenCalledWith('a')

    store.actions = []
    await nextTick()
    expect(wrapper.find('.needs-you').exists()).toBe(false)
    wrapper.unmount()
  })

  it('renders an external link and a dismiss item for ask-style actions', async () => {
    const store = useHousekeepingStore()
    store.actions = [action({
      id: 'github-star',
      link_label: 'Star on GitHub',
      link_url: 'https://github.com/raffaelefarinaro/ciaobot',
      dismiss_label: 'Later',
    })]
    const dismissSpy = vi.spyOn(store, 'dismiss').mockResolvedValue({ ok: true, summary: '' })
    const wrapper = mount(HousekeepingStrip, { global: { plugins: [pinia] } })
    await nextTick()

    const [link] = leads(rows(wrapper)[0])
    expect(link.element.tagName).toBe('A')
    expect(link.attributes('href')).toBe('https://github.com/raffaelefarinaro/ciaobot')
    expect(link.attributes('target')).toBe('_blank')
    expect(link.attributes('rel')).toContain('noopener')
    expect(link.text()).toBe('Star on GitHub')

    await pick(rows(wrapper)[0], 'Later')
    expect(dismissSpy).toHaveBeenCalledWith('github-star')
    wrapper.unmount()
  })

  it('shows a thank-you toast when the star nudge run succeeds', async () => {
    const store = useHousekeepingStore()
    store.actions = [action({
      id: 'github-star',
      run_label: 'I starred it',
      link_label: 'Star on GitHub',
      link_url: 'https://github.com/raffaelefarinaro/ciaobot',
    })]
    vi.spyOn(store, 'run').mockResolvedValue({ ok: true, summary: 'Thank you!' })
    const projectStore = useProjectStore()
    const toastSpy = vi.spyOn(projectStore, 'pushToast').mockImplementation(() => ({ id: 1 }) as never)

    const wrapper = mount(HousekeepingStrip, { global: { plugins: [pinia] } })
    await nextTick()
    // Only the explicit run button records the star; opening the link must not.
    await leads(rows(wrapper)[0])[0].trigger('click')
    expect(toastSpy).toHaveBeenCalledWith(expect.objectContaining({ title: '★ Starred — thank you!' }))
    wrapper.unmount()
  })

  it('does not record a star when the repository link is merely opened', async () => {
    // Opening the page confirms nothing — the visitor may not be signed in or
    // may just be inspecting. The nudge clears through the explicit dismiss.
    const store = useHousekeepingStore()
    store.actions = [action({
      id: 'github-star',
      link_label: 'Star on GitHub',
      link_url: 'https://github.com/raffaelefarinaro/ciaobot',
      dismiss_label: 'Later',
    })]
    const runSpy = vi.spyOn(store, 'run').mockResolvedValue({ ok: true, summary: '' })
    const dismissSpy = vi.spyOn(store, 'dismiss').mockResolvedValue({ ok: true, summary: '' })
    const projectStore = useProjectStore()
    const toastSpy = vi.spyOn(projectStore, 'pushToast').mockImplementation(() => ({ id: 1 }) as never)

    const wrapper = mount(HousekeepingStrip, { global: { plugins: [pinia] } })
    await nextTick()
    await leads(rows(wrapper)[0])[0].trigger('click')
    expect(runSpy).not.toHaveBeenCalled()
    expect(dismissSpy).not.toHaveBeenCalled()
    expect(toastSpy).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('does not run the action when a non-star link is clicked from the menu', async () => {
    // The update row's "Release notes" link must open the notes WITHOUT starting
    // an update the user never asked for.
    const store = useHousekeepingStore()
    store.actions = [action({
      id: 'package-update',
      link_label: 'Release notes',
      link_url: 'https://github.com/raffaelefarinaro/ciaobot/releases/latest',
      run_label: 'Update',
    })]
    const runSpy = vi.spyOn(store, 'run').mockResolvedValue({ ok: true, summary: '' })
    const wrapper = mount(HousekeepingStrip, { global: { plugins: [pinia] } })
    await nextTick()
    const [row] = rows(wrapper)
    expect(leadLabels(row)).toEqual(['Update'])
    await pick(row, 'Release notes')
    expect(runSpy).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})

describe('the menu', () => {
  let pinia: ReturnType<typeof createPinia>

  beforeEach(() => {
    resetHomeNoticeWindows()
    pinia = createPinia()
    setActivePinia(pinia)
    vi.spyOn(useHousekeepingStore(), 'init').mockImplementation(() => {})
    useHousekeepingStore().actions = [
      action({ id: 'a', run_label: 'Run', chat_label: 'Ask', chat_prompt: 'x', dismiss_label: 'Later' }),
    ]
  })

  afterEach(() => {
    document.body.innerHTML = ''
    vi.restoreAllMocks()
  })

  function mountAttached() {
    return mount(HousekeepingStrip, { global: { plugins: [pinia] }, attachTo: document.body })
  }

  it('is a labelled menu button that reports whether it is open', async () => {
    const wrapper = mountAttached()
    await nextTick()
    const trigger = wrapper.get('.needs-you-more')
    expect(trigger.attributes('aria-haspopup')).toBe('menu')
    expect(trigger.attributes('aria-expanded')).toBe('false')
    expect(trigger.attributes('aria-label')).toBe('More actions for A condition needs you')
    expect(wrapper.find('[role="menu"]').exists()).toBe(false)

    await trigger.trigger('click')
    await nextTick()
    expect(trigger.attributes('aria-expanded')).toBe('true')
    expect(trigger.attributes('aria-controls')).toBe(wrapper.get('[role="menu"]').attributes('id'))
    wrapper.unmount()
  })

  it('moves focus into the menu, steps with the arrow keys and wraps', async () => {
    const wrapper = mountAttached()
    await nextTick()
    await wrapper.get('.needs-you-more').trigger('click')
    await nextTick()
    const items = wrapper.findAll('[role="menuitem"]')
    expect(items.map(i => i.text())).toEqual(['Ask', 'Later', 'Hide for now'])
    expect(document.activeElement).toBe(items[0].element)

    await wrapper.get('[role="menu"]').trigger('keydown', { key: 'ArrowDown' })
    expect(document.activeElement).toBe(items[1].element)
    await wrapper.get('[role="menu"]').trigger('keydown', { key: 'End' })
    expect(document.activeElement).toBe(items[2].element)
    await wrapper.get('[role="menu"]').trigger('keydown', { key: 'ArrowDown' })
    expect(document.activeElement).toBe(items[0].element)
    await wrapper.get('[role="menu"]').trigger('keydown', { key: 'ArrowUp' })
    expect(document.activeElement).toBe(items[2].element)
    wrapper.unmount()
  })

  it('closes on Escape and gives focus back to the trigger', async () => {
    const wrapper = mountAttached()
    await nextTick()
    const trigger = wrapper.get('.needs-you-more')
    await trigger.trigger('click')
    await nextTick()
    await wrapper.get('[role="menu"]').trigger('keydown', { key: 'Escape' })
    await nextTick()
    expect(wrapper.find('[role="menu"]').exists()).toBe(false)
    expect(trigger.attributes('aria-expanded')).toBe('false')
    expect(document.activeElement).toBe(trigger.element)
    wrapper.unmount()
  })

  it('closes on an outside press without stealing focus', async () => {
    const wrapper = mountAttached()
    await nextTick()
    await wrapper.get('.needs-you-more').trigger('click')
    await nextTick()
    document.body.dispatchEvent(new Event('pointerdown', { bubbles: true }))
    await nextTick()
    expect(wrapper.find('[role="menu"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('runs the chosen item and closes, returning focus to the trigger', async () => {
    const store = useHousekeepingStore()
    const dismiss = vi.spyOn(store, 'dismiss').mockResolvedValue({ ok: true, summary: '' })
    const wrapper = mountAttached()
    await nextTick()
    await wrapper.get('.needs-you-more').trigger('click')
    await nextTick()
    await wrapper.findAll('[role="menuitem"]').find(i => i.text() === 'Later')!.trigger('click')
    await flushPromises()
    expect(dismiss).toHaveBeenCalledWith('a')
    expect(wrapper.find('[role="menu"]').exists()).toBe(false)
    expect(document.activeElement).toBe(wrapper.get('.needs-you-more').element)
    wrapper.unmount()
  })

  it('is on a health row only because it has something in it', async () => {
    // A health row still carries "Open in Settings"; this pins that the menu is
    // there only because it has something in it.
    const store = useHousekeepingStore()
    store.actions = []
    store.health = { status: 'error', checks: [healthCheck()] }
    const wrapper = mountAttached()
    await nextTick()
    expect(await menuLabels(rows(wrapper)[0])).toEqual(['Open in Settings'])
    wrapper.unmount()
  })
})

describe('workspace health', () => {
  let pinia: ReturnType<typeof createPinia>

  beforeEach(() => {
    resetHomeNoticeWindows()
    pinia = createPinia()
    setActivePinia(pinia)
    vi.spyOn(useHousekeepingStore(), 'init').mockImplementation(() => {})
  })

  afterEach(() => {
    document.body.innerHTML = ''
    vi.restoreAllMocks()
  })

  it('raises a row per error, with its detail and a Fix button', async () => {
    const store = useHousekeepingStore()
    store.health = {
      status: 'error',
      checks: [
        healthCheck(),
        healthCheck({ id: 'vault-writable-work', title: 'Vault for work is not writable', detail: 'Not writable.' }),
      ],
    }
    const wrapper = mount(HousekeepingStrip, { global: { plugins: [pinia] } })
    await nextTick()
    expect(titles(wrapper)).toEqual(['Vault root for work is missing', 'Vault for work is not writable'])
    expect(rows(wrapper)[0].get('.needs-you-line').text()).toBe('The folder does not exist.')
    expect(leadLabels(rows(wrapper)[0])).toEqual(['Fix'])
    expect(rows(wrapper)[0].classes()).toContain('needs-you-row--error')
    // Not colour alone: assistive tech is told it is an error.
    expect(rows(wrapper)[0].get('.sr-only').text()).toContain('Error')
    wrapper.unmount()
  })

  it('does not raise a row for a warning or an ok check', async () => {
    const store = useHousekeepingStore()
    store.health = {
      status: 'warn',
      checks: [healthCheck({ status: 'warn' }), healthCheck({ id: 'fine', status: 'ok' })],
    }
    const wrapper = mount(HousekeepingStrip, { global: { plugins: [pinia] } })
    await nextTick()
    expect(wrapper.find('.needs-you').exists()).toBe(false)
    wrapper.unmount()
  })

  it('sorts health errors above blocking actions above everything else', async () => {
    const store = useHousekeepingStore()
    store.health = { status: 'error', checks: [healthCheck()] }
    store.actions = [
      action({ id: 'plain', title: 'Plain' }),
      action({ id: 'gate', title: 'Gate', blocking: true, run_label: 'Fix now' }),
    ]
    store.updateTasks = [updateTask()]
    store.updateTasksWorkspace = 'personal'
    useProjectStore().activeWorkspace = 'personal'
    const wrapper = mount(HousekeepingStrip, { global: { plugins: [pinia] } })
    await nextTick()
    expect(titles(wrapper)).toEqual([
      'Vault root for work is missing',
      'Gate',
      'Plain',
      'Review the rows your old notes left behind',
    ])
    wrapper.unmount()
  })

  it('Fix posts the existing endpoint through the store and announces the outcome', async () => {
    const store = useHousekeepingStore()
    store.health = { status: 'error', checks: [healthCheck()] }
    const post = vi.spyOn(api, 'post').mockResolvedValue({ status: 'ok', checks: [healthCheck({ status: 'ok' })] })
    const wrapper = mount(HousekeepingStrip, { global: { plugins: [pinia] }, attachTo: document.body })
    await nextTick()
    await leads(rows(wrapper)[0])[0].trigger('click')
    await flushPromises()
    expect(post).toHaveBeenCalledWith('/api/workspace-health/fix', {})
    // The report the endpoint returned replaces the list: the row is gone and
    // the list stays up just long enough to say so.
    expect(rows(wrapper)).toHaveLength(0)
    expect(wrapper.get('.needs-you-status').text()).toBe('Fixed. Workspace health has no errors.')
    // And focus did not fall to the page start with the pressed button.
    expect(document.activeElement).toBe(wrapper.get('.needs-you').element)
    wrapper.unmount()
  })

  it('says how many are left when the fix did not clear them all', async () => {
    const store = useHousekeepingStore()
    store.health = { status: 'error', checks: [healthCheck(), healthCheck({ id: 'b', title: 'B' })] }
    vi.spyOn(api, 'post').mockResolvedValue({ status: 'error', checks: [healthCheck({ id: 'b', title: 'B' })] })
    const wrapper = mount(HousekeepingStrip, { global: { plugins: [pinia] } })
    await nextTick()
    await leads(rows(wrapper)[0])[0].trigger('click')
    await flushPromises()
    expect(titles(wrapper)).toEqual(['B'])
    expect(wrapper.get('.needs-you-status').text()).toContain('1 problem still needs a look in Settings')
    wrapper.unmount()
  })

  it('disables Fix while it runs', async () => {
    const store = useHousekeepingStore()
    store.health = { status: 'error', checks: [healthCheck()] }
    let release: (value: unknown) => void = () => {}
    vi.spyOn(api, 'post').mockImplementation(() => new Promise((resolve) => { release = resolve }))
    const wrapper = mount(HousekeepingStrip, { global: { plugins: [pinia] } })
    await nextTick()
    await leads(rows(wrapper)[0])[0].trigger('click')
    await nextTick()
    const busy = leads(rows(wrapper)[0])[0]
    expect(busy.text()).toBe('Fixing…')
    expect(busy.attributes('disabled')).toBeDefined()
    release({ status: 'ok', checks: [] })
    await flushPromises()
    wrapper.unmount()
  })

  it('keeps the row and shows the refusal as an alert when the fix fails', async () => {
    const store = useHousekeepingStore()
    store.health = { status: 'error', checks: [healthCheck()] }
    vi.spyOn(api, 'post').mockRejectedValue(new Error('failed to repair workspace'))
    vi.spyOn(api, 'get').mockResolvedValue({ status: 'error', checks: [healthCheck()] })
    const wrapper = mount(HousekeepingStrip, { global: { plugins: [pinia] } })
    await nextTick()
    await leads(rows(wrapper)[0])[0].trigger('click')
    await flushPromises()
    expect(rows(wrapper)).toHaveLength(1)
    const line = rows(wrapper)[0].get('.needs-you-line')
    expect(line.attributes('role')).toBe('alert')
    expect(line.text()).toContain('failed to repair workspace')
    wrapper.unmount()
  })

  it('offers Open in Settings, which goes to the Advanced tab', async () => {
    const store = useHousekeepingStore()
    store.health = { status: 'error', checks: [healthCheck()] }
    routerPush.mockClear()
    const wrapper = mount(HousekeepingStrip, { global: { plugins: [pinia] } })
    await nextTick()
    await pick(rows(wrapper)[0], 'Open in Settings')
    await flushPromises()
    expect(routerPush).toHaveBeenCalledWith('/settings/advanced')
    wrapper.unmount()
  })
})

describe('the store reads the health report', () => {
  it('asks /api/workspace-health on init and adopts the report', async () => {
    setActivePinia(createPinia())
    const get = vi.spyOn(api, 'get').mockImplementation(async (path: string) => {
      if (path === '/api/workspace-health') return { status: 'error', checks: [healthCheck(), healthCheck({ id: 'w', status: 'warn' })] }
      if (path === '/api/housekeeping') return { actions: [] }
      return { tasks: [] }
    })
    const store = useHousekeepingStore()
    await store.refreshHealth()
    expect(get).toHaveBeenCalledWith('/api/workspace-health')
    expect(store.healthErrors.map(c => c.id)).toEqual(['vault-root-work'])
    vi.restoreAllMocks()
  })

  it('keeps the previous report when a read fails', async () => {
    setActivePinia(createPinia())
    const store = useHousekeepingStore()
    store.health = { status: 'error', checks: [healthCheck()] }
    vi.spyOn(api, 'get').mockRejectedValue(new Error('offline'))
    await store.refreshHealth()
    expect(store.healthErrors).toHaveLength(1)
    vi.restoreAllMocks()
  })
})

describe('the update row', () => {
  let pinia: ReturnType<typeof createPinia>

  beforeEach(() => {
    pinia = createPinia()
    setActivePinia(pinia)
    vi.spyOn(useHousekeepingStore(), 'init').mockImplementation(() => {})
  })

  afterEach(() => {
    document.body.innerHTML = ''
    vi.restoreAllMocks()
  })

  function updateAction(): OperatorAction {
    return action({
      id: 'package-update',
      kind: 'package-update',
      workspace: '',
      link_label: 'Release notes',
      link_url: 'https://github.com/raffaelefarinaro/ciaobot/releases/latest',
      view_label: 'Update in Settings',
      view_route: '/settings',
      primary: 'view',
    })
  }

  it('leads with the settings button and keeps release notes in the menu', async () => {
    useHousekeepingStore().actions = [updateAction()]
    const wrapper = mount(HousekeepingStrip, { global: { plugins: [pinia] } })
    await nextTick()
    const [row] = rows(wrapper)
    expect(leadLabels(row)).toEqual(['Update in Settings'])
    expect(leads(row)[0].element.tagName).toBe('BUTTON')
    const labels = await menuLabels(row)
    expect(labels).toEqual(['Release notes', 'Hide for now'])
    const notes = row.get('[role="menuitem"]')
    expect(notes.element.tagName).toBe('A')
    expect(notes.attributes('href')).toBe('https://github.com/raffaelefarinaro/ciaobot/releases/latest')
    wrapper.unmount()
  })

  it('leads with the link when the action names no lead', async () => {
    useHousekeepingStore().actions = [{ ...updateAction(), primary: '' }]
    const wrapper = mount(HousekeepingStrip, { global: { plugins: [pinia] } })
    await nextTick()
    const [row] = rows(wrapper)
    expect(leads(row)[0].element.tagName).toBe('A')
    expect(leadLabels(row)).toEqual(['Release notes'])
    expect(await menuLabels(row)).toEqual(['Update in Settings', 'Hide for now'])
    wrapper.unmount()
  })
})

describe('a row that names an existing surface', () => {
  it('leads with a button that navigates there and keeps chat in the menu', async () => {
    // The queue rows offered "Review in chat" alone, so the operator was asked
    // to work through 109 proposals in prose while the panel with per-row
    // accept/dismiss sat one route away.
    const housekeeping = useHousekeepingStore()
    housekeeping.actions = [action({
      id: 'review-queue-depth',
      title: '109 proposals are waiting for a review',
      view_label: 'Open queue',
      view_route: '/proposals',
      chat_label: 'Review in chat',
      chat_prompt: 'discuss it',
    })]
    const wrapper = mount(HousekeepingStrip)
    await nextTick()
    const [row] = rows(wrapper)
    expect(leadLabels(row)).toEqual(['Open queue'])
    expect(await menuLabels(row)).toEqual(['Review in chat', 'Hide for now'])
    routerPush.mockClear()
    await leads(row)[0].trigger('click')
    await flushPromises()
    expect(routerPush).toHaveBeenCalledWith('/proposals')
  })

  it('shows no view button when the action names no surface', async () => {
    const housekeeping = useHousekeepingStore()
    housekeeping.actions = [action({ chat_prompt: 'discuss it', chat_label: 'Discuss' })]
    const wrapper = mount(HousekeepingStrip)
    await nextTick()
    expect(leadLabels(rows(wrapper)[0])).toEqual(['Discuss'])
  })
})

describe('scoping to the active workspace', () => {
  it('hides other workspaces rows, shows shared plus current unlabeled', async () => {
    // Another workspace's pile is not this tab's business.
    const housekeeping = useHousekeepingStore()
    housekeeping.actions = [
      action({ id: 'w', workspace: 'work', title: 'Work thing' }),
      action({ id: 'p', workspace: 'personal', title: 'Personal thing' }),
      action({ id: 's', workspace: '', title: 'Install-wide thing' }),
    ]
    const wrapper = mount(HousekeepingStrip)
    await nextTick()

    expect(titles(wrapper)).toEqual(['Install-wide thing', 'Personal thing'])
    wrapper.unmount()
  })

  it('follows the workspace switcher', async () => {
    const housekeeping = useHousekeepingStore()
    housekeeping.actions = [
      action({ id: 'w', workspace: 'work', title: 'Work thing' }),
      action({ id: 'p', workspace: 'personal', title: 'Personal thing' }),
    ]
    useProjectStore().activeWorkspace = 'work'

    const wrapper = mount(HousekeepingStrip)
    await nextTick()

    expect(titles(wrapper)).toEqual(['Work thing'])
    wrapper.unmount()
  })

  it('names the workspace in the title only when no workspace is active', async () => {
    // With an active workspace, scoping can only leave shared plus that one
    // workspace, which need no labels. The unscoped fallback (no active
    // workspace yet) can mix several, and then they need them.
    const housekeeping = useHousekeepingStore()
    housekeeping.actions = [
      action({ id: 'w', workspace: 'work', title: 'Work thing' }),
      action({ id: 'p', workspace: 'personal', title: 'Personal thing' }),
      action({ id: 's', workspace: '', title: 'Install-wide thing' }),
    ]
    useProjectStore().activeWorkspace = ''

    const wrapper = mount(HousekeepingStrip)
    await nextTick()

    expect(titles(wrapper)).toEqual([
      'Install-wide thing',
      'Personal thing · personal',
      'Work thing · work',
    ])
    wrapper.unmount()
  })
})

describe('a blocking precondition', () => {
  it('reads as an error-toned row, first, with a filled lead', async () => {
    const housekeeping = useHousekeepingStore()
    useProjectStore().activeWorkspace = 'personal'
    housekeeping.actions = [
      action({ id: 'other', title: 'Something optional', run_label: 'Do it' }),
      action({ id: 'gate', blocking: true, title: 'Workspaces still share one vault', run_label: 'Separate them now' }),
    ]
    const wrapper = mount(HousekeepingStrip)
    await nextTick()

    const list = rows(wrapper)
    expect(titles(wrapper)).toEqual(['Workspaces still share one vault', 'Something optional'])
    expect(list[0].classes()).toContain('needs-you-row--blocking')
    expect(list[1].classes()).not.toContain('needs-you-row--blocking')
    expect(list[0].get('.sr-only').text()).toContain('Blocking')
    expect(leads(list[0])[0].classes()).toContain('btn-primary')
    expect(leads(list[1])[0].classes()).not.toContain('btn-primary')
    // It is actionable in place: no navigation required to clear it.
    expect(leadLabels(list[0])).toEqual(['Separate them now'])
  })
})

describe('a provider update row', () => {
  const terminalStub = defineComponent({
    name: 'HostTerminalButton',
    props: { action: { type: String, default: '' }, label: { type: String, default: '' } },
    emits: ['not-applicable'],
    template: '<button class="terminal-stub btn-small" @click="$emit(\'not-applicable\')">{{ label }}</button>',
  })

  function providerUpdate(overrides: Partial<OperatorAction> = {}): OperatorAction {
    return action({
      id: 'provider-update:opencode',
      workspace: '',
      terminal_action: 'opencode-update',
      command: 'opencode upgrade 2.1.0',
      run_label: 'Check again',
      dismiss_label: 'Not now',
      ...overrides,
    })
  }

  function terminalOn(on: boolean) {
    useHostTerminal().capabilities.value = {
      available: on,
      platform: 'darwin',
      reason: on ? '' : 'not_on_host',
      terminal: 'Terminal',
      actions: on ? ['opencode-update'] : [],
    } as never
  }

  function mountStrip() {
    return mount(HousekeepingStrip, { global: { stubs: { HostTerminalButton: terminalStub } } })
  }

  beforeEach(() => {
    resetHostTerminalForTests()
    terminalOn(true)
    vi.spyOn(useHousekeepingStore(), 'init').mockImplementation(() => {})
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
    resetHostTerminalForTests()
  })

  it('leads with the terminal button and moves the rest into the menu', async () => {
    useHousekeepingStore().actions = [providerUpdate()]
    const wrapper = mountStrip()
    await nextTick()

    const terminal = wrapper.getComponent(terminalStub)
    expect(terminal.props('action')).toBe('opencode-update')
    expect(terminal.props('label')).toBe('Update in Terminal')
    const [row] = rows(wrapper)
    expect(leads(row)).toHaveLength(1)
    expect(await menuLabels(row)).toEqual(['Check again', 'Copy command', 'Not now', 'Hide for now'])
  })

  it('leads with Copy command when the terminal is not on this machine', async () => {
    terminalOn(false)
    useHousekeepingStore().actions = [providerUpdate()]
    const wrapper = mountStrip()
    await nextTick()

    expect(wrapper.find('.terminal-stub').exists()).toBe(false)
    const [row] = rows(wrapper)
    expect(leadLabels(row)).toEqual(['Copy command'])
    expect(await menuLabels(row)).toEqual(['Check again', 'Not now', 'Hide for now'])
  })

  it('copies the command and says Copied', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
    useHousekeepingStore().actions = [providerUpdate()]
    const wrapper = mountStrip()
    await nextTick()

    await pick(rows(wrapper)[0], 'Copy command')
    await flushPromises()
    expect(writeText).toHaveBeenCalledWith('opencode upgrade 2.1.0')
    expect(await menuLabels(rows(wrapper)[0])).toContain('Copied')
  })

  it('puts the command on the line, as text, when the clipboard is refused', async () => {
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText: vi.fn().mockRejectedValue(new Error('denied')) } })
    useHousekeepingStore().actions = [providerUpdate()]
    const wrapper = mountStrip()
    await nextTick()

    await pick(rows(wrapper)[0], 'Copy command')
    await flushPromises()
    const line = rows(wrapper)[0].get('.needs-you-line')
    expect(line.text()).toBe('Copy failed. Run: opencode upgrade 2.1.0')
    expect(line.attributes('role')).toBe('alert')
  })

  it('reverts Copied and the failure line after a moment', async () => {
    vi.useFakeTimers()
    try {
      vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } })
      useHousekeepingStore().actions = [providerUpdate()]
      const wrapper = mountStrip()
      await nextTick()
      await pick(rows(wrapper)[0], 'Copy command')
      await flushPromises()
      expect(await menuLabels(rows(wrapper)[0])).toContain('Copied')
      await vi.advanceTimersByTimeAsync(3000)
      expect(await menuLabels(rows(wrapper)[0])).toContain('Copy command')
      wrapper.unmount()
    } finally {
      vi.useRealTimers()
    }
  })

  it('dismisses a provider update with Not now', async () => {
    const store = useHousekeepingStore()
    store.actions = [providerUpdate()]
    const dismiss = vi.spyOn(store, 'dismiss').mockResolvedValue({ ok: true, summary: '' })
    const wrapper = mountStrip()
    await nextTick()

    await pick(rows(wrapper)[0], 'Not now')
    expect(dismiss).toHaveBeenCalledWith('provider-update:opencode')
  })

  it('re-checks from the menu', async () => {
    const store = useHousekeepingStore()
    store.actions = [providerUpdate()]
    const run = vi.spyOn(store, 'run').mockResolvedValue({ ok: true, summary: '' })
    const wrapper = mountStrip()
    await nextTick()

    await pick(rows(wrapper)[0], 'Check again')
    expect(run).toHaveBeenCalledWith('provider-update:opencode')
  })

  it('refreshes the list when the terminal says there is nothing to update', async () => {
    const store = useHousekeepingStore()
    store.actions = [providerUpdate()]
    const refresh = vi.spyOn(store, 'refresh').mockResolvedValue(undefined)
    const wrapper = mountStrip()
    await nextTick()

    await wrapper.get('.terminal-stub').trigger('click')
    expect(refresh).toHaveBeenCalledTimes(1)
  })

  it('shows no terminal button or copy item for actions without a command', async () => {
    useHousekeepingStore().actions = [action({ run_label: 'Run' })]
    const wrapper = mountStrip()
    await nextTick()

    expect(wrapper.find('.terminal-stub').exists()).toBe(false)
    expect(await menuLabels(rows(wrapper)[0])).toEqual(['Hide for now'])
  })

  it('keeps the plain run button as the lead when there is no terminal action', async () => {
    useHousekeepingStore().actions = [action({ run_label: 'Install' })]
    const wrapper = mountStrip()
    await nextTick()

    expect(leadLabels(rows(wrapper)[0])).toEqual(['Install'])
  })
})


function updateTask(overrides: Partial<UpdateTaskRow> = {}): UpdateTaskRow {
  return {
    id: 'review-legacy-rows',
    revision: 1,
    scope: 'workspace',
    title: 'Review the rows your old notes left behind',
    why: 'Rows retired into a folder no surface reads any more.',
    since_version: '2.1.0',
    status: 'offered',
    applicability: 'applicable',
    applicability_checked_at: '2026-09-01T10:00:00+00:00',
    offered: true,
    suppressed: false,
    chat_id: '',
    chat_live: false,
    prompt_digest: '',
    attempted_fingerprint: '',
    updated_at: '',
    ...overrides,
  }
}

describe('notice reveal', () => {
  const Empty = defineComponent({ template: '<div />' })
  const UNAVAILABLE = 'This notice is no longer available'
  const scrollIntoView = vi.fn()
  let pinia: ReturnType<typeof createPinia>

  beforeEach(() => {
    resetHomeNoticeWindows()
    pinia = createPinia()
    setActivePinia(pinia)
    scrollIntoView.mockReset()
    Element.prototype.scrollIntoView = scrollIntoView
    useProjectStore().activeWorkspace = 'personal'
    const housekeeping = useHousekeepingStore()
    vi.spyOn(housekeeping, 'init').mockImplementation(() => {})
    vi.spyOn(housekeeping, 'refresh').mockResolvedValue(undefined)
    vi.spyOn(housekeeping, 'refreshUpdateTasks').mockResolvedValue(undefined)
    housekeeping.actionsHealth = readyHealth()
  })

  async function mountAt(url: string) {
    const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/', component: Empty }] })
    await router.push(url)
    await router.isReady()
    const wrapper = mount(HousekeepingStrip, { global: { plugins: [pinia, router] }, attachTo: document.body })
    await flushPromises()
    return { wrapper, router }
  }

  function noticeEl(key: string): HTMLElement | undefined {
    return [...document.querySelectorAll<HTMLElement>('[data-home-notice-window]')].find(el => el.dataset.noticeKey === key)
  }

  function readyUpdates(rows: UpdateTaskRow[], workspace = 'personal') {
    const store = useHousekeepingStore()
    store.updateTasks = rows
    store.updateTasksWorkspace = workspace
    store.updateTasksHealth = readyHealth()
  }

  const q = (key: string) => `/?notice=${encodeURIComponent(key)}`

  it('reveals_and_focuses_a_closed_action_notice', async () => {
    const key = actionNoticeKey('personal', 'test-action')
    useHousekeepingStore().actions = [action()]
    const windows = useHomeNoticeWindows()
    windows.close(key)
    const { wrapper, router } = await mountAt(q(key))
    expect(windows.isClosed(key)).toBe(false)
    const el = noticeEl(key)!
    expect(el).toBeTruthy()
    expect(document.activeElement).toBe(el)
    expect(scrollIntoView).toHaveBeenCalledTimes(1)
    expect(router.currentRoute.value.fullPath).toBe('/')
    expect(wrapper.text()).not.toContain(UNAVAILABLE)
    wrapper.unmount()
  })

  it('other_closed_notices_stay_closed', async () => {
    const a = actionNoticeKey('personal', 'a')
    const b = actionNoticeKey('personal', 'b')
    useHousekeepingStore().actions = [action({ id: 'a' }), action({ id: 'b' })]
    const windows = useHomeNoticeWindows()
    windows.close(a)
    windows.close(b)
    const { wrapper } = await mountAt(q(a))
    expect(windows.isClosed(a)).toBe(false)
    expect(windows.isClosed(b)).toBe(true)
    expect(noticeEl(b)).toBeUndefined()
    wrapper.unmount()
  })

  it('waits_for_source_ready', async () => {
    const key = actionNoticeKey('personal', 'test-action')
    const store = useHousekeepingStore()
    store.actionsHealth = { ...readyHealth(), status: 'checking' }
    store.actions = []
    const windows = useHomeNoticeWindows()
    const { wrapper } = await mountAt(q(key))
    expect(wrapper.text()).not.toContain(UNAVAILABLE)
    expect(windows.pendingReveal.value).toBe(key)
    store.actions = [action()]
    store.actionsHealth = readyHealth()
    await flushPromises()
    expect(windows.pendingReveal.value).toBe('')
    expect(document.activeElement).toBe(noticeEl(key))
    expect(wrapper.text()).not.toContain(UNAVAILABLE)
    wrapper.unmount()
  })

  it.each([
    ['unknown action id', actionNoticeKey('personal', 'nope'), []],
    ['wrong-workspace action', actionNoticeKey('work', 'x'), []],
    ['malformed key', 'garbage', []],
    ['dismissed update task', updateNoticeKey('personal', 'review-legacy-rows', 1), [updateTask({ status: 'dismissed' })]],
    ['completed update task', updateNoticeKey('personal', 'review-legacy-rows', 1), [updateTask({ status: 'completed' })]],
    ['not applicable update offer', updateNoticeKey('personal', 'review-legacy-rows', 1), [updateTask({ applicability: 'not_applicable' })]],
  ])('unavailable_for_missing_keys %s', async (_name, key, tasks) => {
    const store = useHousekeepingStore()
    store.actions = [action({ id: 'x', workspace: 'personal' }), action({ id: 'other' })]
    readyUpdates(tasks as UpdateTaskRow[])
    const windows = useHomeNoticeWindows()
    const closed = actionNoticeKey('personal', 'other')
    windows.close(closed)
    const { wrapper } = await mountAt(q(key as string))
    expect(wrapper.get('.home-notice-unavailable').text()).toBe(UNAVAILABLE)
    expect(windows.isClosed(closed)).toBe(true)
    expect(windows.pendingReveal.value).toBe('')
    expect(scrollIntoView).not.toHaveBeenCalled()
    expect(document.activeElement).toBe(document.body)
    wrapper.unmount()
  })

  it('previous_workspace_update_key_cannot_target_new_workspace', async () => {
    useProjectStore().activeWorkspace = 'work'
    readyUpdates([updateTask()], 'work')
    const windows = useHomeNoticeWindows()
    const workKey = updateNoticeKey('work', 'review-legacy-rows', 1)
    windows.close(workKey)
    const { wrapper } = await mountAt(q(updateNoticeKey('personal', 'review-legacy-rows', 1)))
    expect(wrapper.get('.home-notice-unavailable').text()).toBe(UNAVAILABLE)
    expect(windows.isClosed(workKey)).toBe(true)
    wrapper.unmount()
  })

  it('reveals_update_task_notice', async () => {
    const key = updateNoticeKey('personal', 'review-legacy-rows', 1)
    readyUpdates([updateTask()])
    const windows = useHomeNoticeWindows()
    windows.close(key)
    const { wrapper } = await mountAt(q(key))
    expect(windows.isClosed(key)).toBe(false)
    expect(document.activeElement).toBe(noticeEl(key))
    wrapper.unmount()
  })

  it('waits_for_update_tasks_source', async () => {
    const key = updateNoticeKey('personal', 'review-legacy-rows', 1)
    const store = useHousekeepingStore()
    const refreshUpdates = vi.mocked(store.refreshUpdateTasks)
    const { wrapper } = await mountAt(q(key))
    expect(wrapper.text()).not.toContain(UNAVAILABLE)
    expect(refreshUpdates).toHaveBeenCalledTimes(1)
    expect(refreshUpdates).toHaveBeenCalledWith('personal')
    readyUpdates([updateTask()])
    await flushPromises()
    expect(refreshUpdates).toHaveBeenCalledTimes(1)
    expect(document.activeElement).toBe(noticeEl(key))
    wrapper.unmount()
  })

  it('never_acts', async () => {
    const key = actionNoticeKey('personal', 'test-action')
    const store = useHousekeepingStore()
    store.actions = [action({ run_label: 'Run', dismiss_label: 'Later', chat_prompt: 'x' })]
    readyUpdates([updateTask()])
    const spies = [
      vi.spyOn(store, 'run'),
      vi.spyOn(store, 'dismiss'),
      vi.spyOn(store, 'startUpdateTask'),
      vi.spyOn(store, 'dismissUpdateTask'),
      vi.spyOn(api, 'post'),
      vi.spyOn(confirmModule, 'askConfirm'),
    ]
    const { wrapper, router } = await mountAt(q(key))
    const push = vi.spyOn(router, 'push')
    for (const spy of spies) expect(spy).not.toHaveBeenCalled()
    expect(push).not.toHaveBeenCalled()
    expect(router.currentRoute.value.path).toBe('/')
    wrapper.unmount()
  })

  it('first_value_of_repeated_or_empty_query', async () => {
    const a = actionNoticeKey('personal', 'a')
    useHousekeepingStore().actions = [action({ id: 'a' })]
    const windows = useHomeNoticeWindows()
    const repeated = await mountAt(`/?notice=${encodeURIComponent(a)}&notice=b&keep=1#frag`)
    expect(document.activeElement).toBe(noticeEl(a))
    expect(repeated.router.currentRoute.value.fullPath).toBe('/?keep=1#frag')
    repeated.wrapper.unmount()

    const empty = await mountAt('/?notice=&keep=2')
    expect(windows.pendingReveal.value).toBe('')
    expect(empty.wrapper.text()).not.toContain(UNAVAILABLE)
    expect(empty.router.currentRoute.value.fullPath).toBe('/?keep=2')
    empty.wrapper.unmount()
  })

  it('data_notice_key_matches_lib_builder', async () => {
    useHousekeepingStore().actions = [action({ id: 'a' }), action({ id: 'g', workspace: '' })]
    readyUpdates([updateTask()])
    const { wrapper } = await mountAt('/')
    const keys = [...document.querySelectorAll<HTMLElement>('[data-home-notice-window]')].map(el => el.dataset.noticeKey)
    expect(keys).toEqual(expect.arrayContaining([
      actionNoticeKey('personal', 'a'),
      actionNoticeKey('', 'g'),
      updateNoticeKey('personal', 'review-legacy-rows', 1),
    ]))
    wrapper.unmount()
  })
})
