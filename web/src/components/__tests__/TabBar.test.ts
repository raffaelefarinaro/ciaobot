// @vitest-environment jsdom

import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import TabBar from '../TabBar.vue'

const TABS = [
  { key: 'queue', label: 'Queue' },
  { key: 'history', label: 'History', count: 12 },
  { key: 'trash', label: 'Trash' },
]

function mountBar(modelValue = 'queue') {
  return mount(TabBar, {
    props: { modelValue, tabs: TABS, label: 'Review', idPrefix: 'x' },
  })
}

describe('TabBar', () => {
  it('exposes the ARIA tablist contract', () => {
    const wrapper = mountBar('history')
    const bar = wrapper.find('[role="tablist"]')
    expect(bar.attributes('aria-label')).toBe('Review')

    const tabs = wrapper.findAll('[role="tab"]')
    expect(tabs).toHaveLength(3)
    expect(tabs[1]!.attributes('aria-selected')).toBe('true')
    expect(tabs[0]!.attributes('aria-selected')).toBe('false')
    // Each tab names the panel it controls, and the ids follow the prefix so a
    // second bar on the same page cannot collide with this one.
    expect(tabs[1]!.attributes('id')).toBe('x-tab-history')
    expect(tabs[1]!.attributes('aria-controls')).toBe('x-panel-history')
  })

  it('is a single Tab stop: only the selected tab is reachable by Tab', () => {
    const wrapper = mountBar('history')
    const tabs = wrapper.findAll('[role="tab"]')
    expect(tabs.map(t => t.attributes('tabindex'))).toEqual(['-1', '0', '-1'])
  })

  it('moves and selects with Left/Right, wrapping at both ends', async () => {
    // The behaviour the Memory Map's review tabs shipped without: they had
    // role="tab" and aria-selected but no arrow-key handling at all, so the
    // bar was reachable but not operable from the keyboard.
    const wrapper = mountBar('queue')
    const tabs = wrapper.findAll('[role="tab"]')

    await tabs[0]!.trigger('keydown', { key: 'ArrowRight' })
    expect(wrapper.emitted('update:modelValue')!.at(-1)).toEqual(['history'])

    await tabs[0]!.trigger('keydown', { key: 'ArrowLeft' })
    expect(wrapper.emitted('update:modelValue')!.at(-1)).toEqual(['trash'])
  })

  it('jumps to the ends with Home and End', async () => {
    const wrapper = mountBar('history')
    const tabs = wrapper.findAll('[role="tab"]')

    await tabs[1]!.trigger('keydown', { key: 'Home' })
    expect(wrapper.emitted('update:modelValue')!.at(-1)).toEqual(['queue'])

    await tabs[1]!.trigger('keydown', { key: 'End' })
    expect(wrapper.emitted('update:modelValue')!.at(-1)).toEqual(['trash'])
  })

  it('leaves other keys to the browser', async () => {
    const wrapper = mountBar()
    await wrapper.findAll('[role="tab"]')[0]!.trigger('keydown', { key: 'a' })
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
  })

  it('renders a count only when one is given', () => {
    const wrapper = mountBar()
    const counts = wrapper.findAll('.tab-bar-count')
    expect(counts).toHaveLength(1)
    expect(counts[0]!.text()).toBe('12')
  })

  it('hides the pill for a null count, which means "still loading"', () => {
    const wrapper = mount(TabBar, {
      props: {
        modelValue: 'history',
        tabs: [{ key: 'history', label: 'History', count: null }],
        label: 'Review',
        idPrefix: 'x',
      },
    })
    expect(wrapper.find('.tab-bar-count').exists()).toBe(false)
  })

  it('does not re-emit for the tab that is already selected', async () => {
    const wrapper = mountBar('queue')
    await wrapper.findAll('[role="tab"]')[0]!.trigger('click')
    expect(wrapper.emitted('update:modelValue')).toBeUndefined()
  })
})
