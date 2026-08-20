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

describe('grouping by workspace', () => {
  it('groups tiles by workspace with shared ones first', async () => {
    // An action's workspace decides where acting on it writes, so a flat strip
    // made the reader parse each tile's prose to work out which one it was about.
    const housekeeping = useHousekeepingStore()
    housekeeping.actions = [
      action({ id: 'w', workspace: 'work', title: 'Work thing' }),
      action({ id: 'p', workspace: 'personal', title: 'Personal thing' }),
      action({ id: 's', workspace: '', title: 'Install-wide thing' }),
    ]
    const wrapper = mount(HousekeepingStrip)
    await nextTick()

    expect(wrapper.findAll('.housekeeping-group').map((h) => h.text()))
      .toEqual(['shared', 'personal', 'work'])
  })

  it('shows no heading when nothing distinguishes the tiles', async () => {
    // One install, nothing workspace-specific: labelling it "shared" is noise.
    const housekeeping = useHousekeepingStore()
    housekeeping.actions = [action({ workspace: '' })]
    const wrapper = mount(HousekeepingStrip)
    await nextTick()

    expect(wrapper.findAll('.housekeeping-group')).toHaveLength(0)
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
