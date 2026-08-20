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
