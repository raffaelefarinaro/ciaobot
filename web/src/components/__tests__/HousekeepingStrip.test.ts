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
import { readyHealth } from '../../lib/sourceHealth'
import { useProjectStore } from '../../stores/projects'
import { useHousekeepingStore } from '../../stores/housekeeping'
import type { OperatorAction, UpdateTaskRow } from '../../lib/types'

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

  it('renders nothing at all with zero actions (no wrapper element)', () => {
    const store = useHousekeepingStore()
    store.actions = []
    const wrapper = mount(HousekeepingStrip, { global: { plugins: [pinia] } })
    // No wrapper element, no heading, no "all clear" row. The v-if renders a
    // placeholder comment, not a real DOM element.
    expect(wrapper.find('.housekeeping').exists()).toBe(false)
    expect(wrapper.find('section').exists()).toBe(false)
    expect(wrapper.findAll('*').length).toBe(0)
    wrapper.unmount()
  })

  it('renders a tile per action with run and chat buttons as offered', async () => {
    const store = useHousekeepingStore()
    store.actions = [
      action({ id: 'a', run_label: 'Install', chat_prompt: '' }),
      action({ id: 'b', run_label: '', chat_label: 'Fix in chat', chat_prompt: 'do it' }),
      action({ id: 'c', run_label: 'Run', chat_prompt: 'talk' }),
    ]
    const wrapper = mount(HousekeepingStrip, { global: { plugins: [pinia] } })
    await nextTick()
    const tiles = wrapper.findAll('.housekeeping-tile')
    expect(tiles.length).toBe(3)

    // Run-only tile: no chat button.
    const aButtons = tiles[0].findAll('.housekeeping-actions button')
    expect(aButtons.length).toBe(1)
    expect(aButtons[0].text()).toBe('Install')

    // Chat-only tile: one button seeded from chat_label.
    const bButtons = tiles[1].findAll('.housekeeping-actions button')
    expect(bButtons.length).toBe(1)
    expect(bButtons[0].text()).toBe('Fix in chat')

    // Both-buttons tile.
    const cButtons = tiles[2].findAll('.housekeeping-actions button')
    expect(cButtons.length).toBe(2)
    wrapper.unmount()
  })

  it('closes one window without dismissing its action and reopens it on Home', async () => {
    const store = useHousekeepingStore()
    store.actions = [action({ id: 'a' }), action({ id: 'b', title: 'Another condition' })]
    const wrapper = mount(HousekeepingStrip, { global: { plugins: [pinia] } })
    const reopen = mount(HomeNoticeReopen)
    await nextTick()
    await wrapper.get('[aria-label="Close A condition needs you window"]').trigger('click')
    await nextTick()
    expect(wrapper.findAll('.housekeeping-tile')).toHaveLength(1)
    expect(store.actions).toHaveLength(2)
    expect(reopen.text()).toBe('Show 1 closed notice')
    await reopen.get('button').trigger('click')
    await nextTick()
    expect(wrapper.findAll('.housekeeping-tile')).toHaveLength(2)
    reopen.unmount()
    wrapper.unmount()
  })

  it('run button calls run and re-renders from the response list', async () => {
    const store = useHousekeepingStore()
    store.actions = [action({ id: 'a', run_label: 'Run' })]
    const runSpy = vi.spyOn(store, 'run').mockResolvedValue({ ok: true, summary: '' })
    const wrapper = mount(HousekeepingStrip, { global: { plugins: [pinia] } })
    await nextTick()
    const runButton = wrapper.find('.housekeeping-actions button')
    expect(runButton.text()).toBe('Run')
    await runButton.trigger('click')
    expect(runSpy).toHaveBeenCalledWith('a')

    // Response replaced the list: now empty, so the strip collapses.
    store.actions = []
    await nextTick()
    expect(wrapper.find('.housekeeping').exists()).toBe(false)
    wrapper.unmount()
  })

  it('renders an external link and a dismiss button for ask-style actions', async () => {
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

    const link = wrapper.find('a.housekeeping-link')
    expect(link.exists()).toBe(true)
    expect(link.attributes('href')).toBe('https://github.com/raffaelefarinaro/ciaobot')
    expect(link.attributes('target')).toBe('_blank')
    expect(link.text()).toBe('Star on GitHub')

    const later = wrapper.findAll('button').find((b) => b.text() === 'Later')
    expect(later).toBeTruthy()
    await later!.trigger('click')
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
    const { useProjectStore } = await import('../../stores/projects')
    const projectStore = useProjectStore()
    const toastSpy = vi.spyOn(projectStore, 'pushToast').mockImplementation(() => ({ id: 1 }) as never)

    const wrapper = mount(HousekeepingStrip, { global: { plugins: [pinia] } })
    await nextTick()
    // Only the strip's explicit run button records the star; the link click
    // must not (issue: opening the repo confirms nothing).
    await wrapper.findAll('button').find((b) => b.text() === 'I starred it')!.trigger('click')
    expect(toastSpy).toHaveBeenCalledWith(expect.objectContaining({ title: '★ Starred — thank you!' }))
    wrapper.unmount()
  })

  it('does not record a star when the repository link is merely opened', async () => {
    // Clicking "Star on GitHub" used to record the star and dismiss the
    // nudge permanently, but opening the page confirms nothing — the visitor
    // may not be signed in or may just be inspecting. The link must only
    // open the page; the tile clears through the explicit "Later" dismiss.
    const store = useHousekeepingStore()
    store.actions = [action({
      id: 'github-star',
      link_label: 'Star on GitHub',
      link_url: 'https://github.com/raffaelefarinaro/ciaobot',
      dismiss_label: 'Later',
    })]
    const runSpy = vi.spyOn(store, 'run').mockResolvedValue({ ok: true, summary: '' })
    const { useProjectStore } = await import('../../stores/projects')
    const projectStore = useProjectStore()
    const toastSpy = vi.spyOn(projectStore, 'pushToast').mockImplementation(() => ({ id: 1 }) as never)

    const wrapper = mount(HousekeepingStrip, { global: { plugins: [pinia] } })
    await nextTick()
    await wrapper.find('a.housekeeping-link').trigger('click')
    expect(runSpy).not.toHaveBeenCalled()
    expect(toastSpy).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('does not run the action when a non-star link is clicked', async () => {
    // The update tile's "Release notes" link must open the notes WITHOUT
    // starting an update the user never asked for.
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
    await wrapper.find('a.housekeeping-link').trigger('click')
    expect(runSpy).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})

describe('the update tile', () => {
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

  function controls(wrapper: ReturnType<typeof mount>) {
    return wrapper.findAll('.housekeeping-actions > *')
  }

  it('leads with the filled settings button and demotes release notes', async () => {
    useHousekeepingStore().actions = [updateAction()]
    const wrapper = mount(HousekeepingStrip, { global: { plugins: [pinia] } })
    await nextTick()

    const [first, second] = controls(wrapper)
    expect(controls(wrapper)).toHaveLength(2)
    expect(first.element.tagName).toBe('BUTTON')
    expect(first.text()).toBe('Update in Settings')
    expect(first.classes()).toContain('btn-primary')
    expect(first.classes()).not.toContain('btn-chip')
    expect(second.element.tagName).toBe('A')
    expect(second.text()).toBe('Release notes')
    expect(second.classes()).toContain('btn-chip')
    expect(second.classes()).not.toContain('btn-primary')
    wrapper.unmount()
  })

  it('keeps the default order when the action names no lead', async () => {
    useHousekeepingStore().actions = [{ ...updateAction(), primary: '' }]
    const wrapper = mount(HousekeepingStrip, { global: { plugins: [pinia] } })
    await nextTick()

    const [first, second] = controls(wrapper)
    expect(first.element.tagName).toBe('A')
    expect(first.classes()).toContain('btn-primary')
    expect(second.element.tagName).toBe('BUTTON')
    expect(second.classes()).toContain('btn-primary')
    wrapper.unmount()
  })
})

describe('a tile that names an existing surface', () => {
  it('offers a button that navigates there, not only chat', async () => {
    // The queue tiles offered "Review in chat" alone, so the operator was asked
    // to work through 109 proposals in prose while the panel with per-row
    // accept/dismiss, a destination picker and batch operations sat one route
    // away, unreachable from the only place that mentions them.
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

    const labels = wrapper.findAll('button').map((b) => b.text())
    expect(labels).toContain('Open queue')
    expect(labels).toContain('Review in chat')
  })

  it('shows no view button when the action names no surface', async () => {
    const housekeeping = useHousekeepingStore()
    housekeeping.actions = [action({ chat_prompt: 'discuss it', chat_label: 'Discuss' })]
    const wrapper = mount(HousekeepingStrip)
    await nextTick()

    expect(wrapper.findAll('.housekeeping-actions button').map((b) => b.text())).toEqual(['Discuss'])
  })
})

describe('scoping to the active workspace', () => {
  const tileTitles = (wrapper: ReturnType<typeof mount>) =>
    wrapper.findAll('.home-notice-title').map((t) => t.text())

  it('hides other workspaces tiles, shows shared plus current unlabeled', async () => {
    // Another workspace's pile is not this tab's business. The strip used to
    // sum every workspace's queue into one tile, so the review tile claimed
    // proposals for a tab whose own queue held fewer — while /proposals, the
    // page the tile opens, scopes its rows to the active workspace.
    const housekeeping = useHousekeepingStore()
    housekeeping.actions = [
      action({ id: 'w', workspace: 'work', title: 'Work thing' }),
      action({ id: 'p', workspace: 'personal', title: 'Personal thing' }),
      action({ id: 's', workspace: '', title: 'Install-wide thing' }),
    ]
    const wrapper = mount(HousekeepingStrip)
    await nextTick()

    expect(tileTitles(wrapper)).toEqual(['Install-wide thing', 'Personal thing'])
    // Both surviving groups concern the reader's own position or the whole
    // install, so headings would be noise.
    expect(wrapper.findAll('.housekeeping-group')).toHaveLength(0)
    wrapper.unmount()
  })

  it('follows the workspace switcher', async () => {
    const housekeeping = useHousekeepingStore()
    housekeeping.actions = [
      action({ id: 'w', workspace: 'work', title: 'Work thing' }),
      action({ id: 'p', workspace: 'personal', title: 'Personal thing' }),
    ]
    const { useProjectStore } = await import('../../stores/projects')
    const projectStore = useProjectStore()
    projectStore.activeWorkspace = 'work'

    const wrapper = mount(HousekeepingStrip)
    await nextTick()

    expect(tileTitles(wrapper)).toEqual(['Work thing'])
    wrapper.unmount()
  })

  it('labels named groups only when no workspace is active', async () => {
    // With an active workspace, scoping can only leave shared plus that one
    // workspace, which need no labels. The unscoped fallback (no active
    // workspace yet) can mix several named groups, and then they need them.
    const housekeeping = useHousekeepingStore()
    housekeeping.actions = [
      action({ id: 'w', workspace: 'work', title: 'Work thing' }),
      action({ id: 'p', workspace: 'personal', title: 'Personal thing' }),
      action({ id: 's', workspace: '', title: 'Install-wide thing' }),
    ]
    const { useProjectStore } = await import('../../stores/projects')
    const projectStore = useProjectStore()
    projectStore.activeWorkspace = ''

    const wrapper = mount(HousekeepingStrip)
    await nextTick()

    expect(tileTitles(wrapper)).toEqual([
      'Install-wide thing',
      'Personal thing',
      'Work thing',
    ])
    expect(wrapper.findAll('.housekeeping-group').map((h) => h.text()))
      .toEqual(['shared', 'personal', 'work'])
    wrapper.unmount()
  })
})

describe('a blocking precondition', () => {
  it('renders as a warning rather than one tile among several', async () => {
    const housekeeping = useHousekeepingStore()
    housekeeping.actions = [
      action({ id: 'gate', blocking: true, title: 'Workspaces still share one vault', run_label: 'Separate them now' }),
      action({ id: 'other', title: 'Something optional' }),
    ]
    const wrapper = mount(HousekeepingStrip)
    await nextTick()

    const tiles = wrapper.findAll('.housekeeping-tile')
    expect(tiles[0].classes()).toContain('housekeeping-tile--blocking')
    expect(tiles[1].classes()).not.toContain('housekeeping-tile--blocking')
    // It is actionable in place: no navigation required to clear it.
    expect(tiles[0].text()).toContain('Separate them now')
  })
})

describe('a provider update tile', () => {
  const terminalStub = defineComponent({
    name: 'HostTerminalButton',
    props: { action: { type: String, default: '' }, label: { type: String, default: '' } },
    emits: ['not-applicable'],
    template: '<button class="terminal-stub" @click="$emit(\'not-applicable\')">{{ label }}</button>',
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

  function mountStrip() {
    return mount(HousekeepingStrip, { global: { stubs: { HostTerminalButton: terminalStub } } })
  }

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('renders the copyable command and the terminal button for a provider update', async () => {
    useHousekeepingStore().actions = [providerUpdate()]
    const wrapper = mountStrip()
    await nextTick()

    expect(wrapper.get('.housekeeping-command code').text()).toBe('opencode upgrade 2.1.0')
    const terminal = wrapper.getComponent(terminalStub)
    expect(terminal.props('action')).toBe('opencode-update')
    expect(terminal.props('label')).toBe('Update in Terminal')
    const checkAgain = wrapper.findAll('.housekeeping-actions button').find(b => b.text() === 'Check again')!
    expect(checkAgain.classes()).toContain('btn-chip')
    expect(checkAgain.classes()).not.toContain('btn-primary')
  })

  it('copies the command and says Copied', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText } })
    useHousekeepingStore().actions = [providerUpdate()]
    const wrapper = mountStrip()
    await nextTick()

    const copy = wrapper.get('.housekeeping-command button')
    expect(copy.text()).toBe('Copy')
    await copy.trigger('click')
    await flushPromises()
    expect(writeText).toHaveBeenCalledWith('opencode upgrade 2.1.0')
    expect(copy.text()).toBe('Copied')
  })

  it('says the copy failed, in text, when the clipboard is refused', async () => {
    vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText: vi.fn().mockRejectedValue(new Error('denied')) } })
    useHousekeepingStore().actions = [providerUpdate()]
    const wrapper = mountStrip()
    await nextTick()

    await wrapper.get('.housekeeping-command button').trigger('click')
    await flushPromises()
    expect(wrapper.get('.housekeeping-command [role=status]').text()).toBe('Copy failed; select the command above.')
    expect(wrapper.get('.housekeeping-command button').text()).toBe('Copy')
  })

  it('reverts Copied and the failure note after a moment', async () => {
    vi.useFakeTimers()
    try {
      vi.stubGlobal('navigator', { ...navigator, clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } })
      useHousekeepingStore().actions = [providerUpdate()]
      const wrapper = mountStrip()
      await nextTick()
      const copy = wrapper.get('.housekeeping-command button')
      await copy.trigger('click')
      await flushPromises()
      expect(copy.text()).toBe('Copied')
      await vi.advanceTimersByTimeAsync(3000)
      expect(copy.text()).toBe('Copy')
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

    await wrapper.findAll('.housekeeping-actions button').find(b => b.text() === 'Not now')!.trigger('click')
    expect(dismiss).toHaveBeenCalledWith('provider-update:opencode')
  })

  it('refreshes the strip when the terminal says there is nothing to update', async () => {
    const store = useHousekeepingStore()
    store.actions = [providerUpdate()]
    const refresh = vi.spyOn(store, 'refresh').mockResolvedValue(undefined)
    const wrapper = mountStrip()
    await nextTick()

    await wrapper.get('.terminal-stub').trigger('click')
    expect(refresh).toHaveBeenCalledTimes(1)
  })

  it('shows no command row or terminal button for actions without one', async () => {
    useHousekeepingStore().actions = [action({ run_label: 'Run' })]
    const wrapper = mountStrip()
    await nextTick()

    expect(wrapper.find('.housekeeping-command').exists()).toBe(false)
    expect(wrapper.find('.terminal-stub').exists()).toBe(false)
  })

  it('keeps the filled run button when there is no terminal action', async () => {
    useHousekeepingStore().actions = [action({ run_label: 'Install' })]
    const wrapper = mountStrip()
    await nextTick()

    expect(wrapper.get('.housekeeping-actions button').classes()).toContain('btn-primary')
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
