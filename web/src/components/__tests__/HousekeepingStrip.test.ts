// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import HousekeepingStrip from '../HousekeepingStrip.vue'
import { useHousekeepingStore } from '../../stores/housekeeping'
import type { OperatorAction } from '../../lib/types'

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
    const aButtons = tiles[0].findAll('button')
    expect(aButtons.length).toBe(1)
    expect(aButtons[0].text()).toBe('Install')

    // Chat-only tile: one button seeded from chat_label.
    const bButtons = tiles[1].findAll('button')
    expect(bButtons.length).toBe(1)
    expect(bButtons[0].text()).toBe('Fix in chat')

    // Both-buttons tile.
    const cButtons = tiles[2].findAll('button')
    expect(cButtons.length).toBe(2)
    wrapper.unmount()
  })

  it('run button calls run and re-renders from the response list', async () => {
    const store = useHousekeepingStore()
    store.actions = [action({ id: 'a', run_label: 'Run' })]
    const runSpy = vi.spyOn(store, 'run').mockResolvedValue({ ok: true, summary: '' })
    const wrapper = mount(HousekeepingStrip, { global: { plugins: [pinia] } })
    await nextTick()
    const runButton = wrapper.find('button')
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

    expect(wrapper.findAll('button').map((b) => b.text())).toEqual(['Discuss'])
  })
})

describe('scoping to the active workspace', () => {
  const tileTitles = (wrapper: ReturnType<typeof mount>) =>
    wrapper.findAll('.housekeeping-title').map((t) => t.text())

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
