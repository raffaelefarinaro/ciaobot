// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import type { RunningWorkRow } from '../../lib/runningWork'
import ComposerActivity from '../ComposerActivity.vue'

const NOW = Date.parse('2026-11-20T10:10:00Z')

const AGENT: RunningWorkRow = { tool_use_id: 'toolu_a1', label: 'Explore repo layout', detail: 'Explore', started_at: NOW - 65_000 }
const AGENT_2: RunningWorkRow = { tool_use_id: 'toolu_a2', label: 'Write the migration', detail: '', started_at: NOW - 5_000 }
const SHELL: RunningWorkRow = { tool_use_id: 'toolu_s1', label: 'npm run dev', detail: '', started_at: NOW - 120_000 }
const SHELL_2: RunningWorkRow = { tool_use_id: 'toolu_s2', label: 'ping -c 8 127.0.0.1', detail: '' }

function mountBar(props: { agents?: RunningWorkRow[]; shells?: RunningWorkRow[]; now?: number } = {}) {
  return mount(ComposerActivity, {
    attachTo: document.body,
    props: { chatId: 'c1', agents: [], shells: [], now: NOW, ...props },
  })
}

// Reka renders the popover content in its own layer; read it through document.
const popover = () => document.querySelector<HTMLElement>('.composer-activity-popover')
const chipText = (wrapper: ReturnType<typeof mountBar>, cls: string) => wrapper.get(cls).text()

async function open(wrapper: ReturnType<typeof mountBar>, cls: string) {
  await wrapper.get(cls).trigger('click')
  await flushPromises()
}

afterEach(() => {
  vi.unstubAllGlobals()
  document.body.innerHTML = ''
})

describe('ComposerActivity chips', () => {
  it('shows no chip and no popover trigger when nothing runs', () => {
    const wrapper = mountBar()
    expect(wrapper.findAll('.composer-activity-seg')).toHaveLength(0)
    expect(wrapper.find('.composer-activity-segs').exists()).toBe(false)
    wrapper.unmount()
  })

  it('labels one agent in the singular with its working glyph', () => {
    const wrapper = mountBar({ agents: [AGENT] })
    expect(chipText(wrapper, '.composer-activity-seg--agents')).toBe('1 agent')
    expect(wrapper.get('.composer-activity-seg--agents').attributes('title')).toBe('1 agent running')
    expect(wrapper.get('.composer-activity-seg--agents').find('.activity-glyph--working').exists()).toBe(true)
    expect(wrapper.find('.composer-activity-seg--runs').exists()).toBe(false)
    wrapper.unmount()
  })

  it('labels shells only, in the plural, with the run glyph', () => {
    const wrapper = mountBar({ shells: [SHELL, SHELL_2] })
    expect(wrapper.find('.composer-activity-seg--agents').exists()).toBe(false)
    expect(chipText(wrapper, '.composer-activity-seg--runs')).toBe('2 shells')
    expect(wrapper.get('.composer-activity-seg--runs').attributes('title')).toBe('2 shells running')
    expect(wrapper.get('.composer-activity-seg--runs').find('.activity-glyph--run').exists()).toBe(true)
    wrapper.unmount()
  })

  it('shows both chips, agents first, when both run', () => {
    const wrapper = mountBar({ agents: [AGENT, AGENT_2], shells: [SHELL] })
    const segs = wrapper.findAll('.composer-activity-seg').map(seg => seg.classes().join(' '))
    expect(segs[0]).toContain('composer-activity-seg--agents')
    expect(segs[1]).toContain('composer-activity-seg--runs')
    expect(chipText(wrapper, '.composer-activity-seg--agents')).toBe('2 agents')
    expect(chipText(wrapper, '.composer-activity-seg--runs')).toBe('1 shell')
    wrapper.unmount()
  })

  it('folds to the glyph and a count in compact mode', async () => {
    vi.stubGlobal('ResizeObserver', class {
      constructor(private readonly callback: (entries: Array<{ contentRect: { width: number } }>) => void) {}
      observe(target: Element) {
        this.callback([{ contentRect: { width: 320 }, target } as unknown as { contentRect: { width: number } }])
      }
      disconnect() {}
    })
    const wrapper = mount(ComposerActivity, {
      attachTo: document.body,
      props: { chatId: 'c1', agents: [AGENT, AGENT_2], shells: [SHELL], now: NOW },
    })
    await flushPromises()
    expect(wrapper.find('.composer-activity--compact').exists()).toBe(true)
    expect(wrapper.get('.composer-activity-seg--agents .composer-activity-count').text()).toBe('2')
    expect(wrapper.get('.composer-activity-seg--runs .composer-activity-count').text()).toBe('1')
    expect(wrapper.find('.composer-activity-label').exists()).toBe(false)
    wrapper.unmount()
  })
})

describe('ComposerActivity popover', () => {
  it('lists each running row with its elapsed time from the shared clock', async () => {
    const wrapper = mountBar({ agents: [AGENT, AGENT_2], shells: [SHELL] })
    await open(wrapper, '.composer-activity-seg--agents')
    const agentRows = Array.from(popover()!.querySelectorAll('.composer-activity-pop-list')[0].querySelectorAll('li'))
    expect(agentRows.map(li => li.querySelector('.composer-activity-pop-label')?.textContent)).toEqual([
      'Explore repo layout', 'Write the migration',
    ])
    expect(agentRows[0].querySelector('.composer-activity-pop-detail')?.textContent).toBe('Explore')
    expect(agentRows[0].querySelector('.composer-activity-pop-age')?.textContent).toBe('1m 5s')
    expect(agentRows[1].querySelector('.composer-activity-pop-age')?.textContent).toBe('5s')
    wrapper.unmount()
  })

  it('lists shells under their own heading, with no stop or transcript control in the rows', async () => {
    const wrapper = mountBar({ shells: [SHELL] })
    await open(wrapper, '.composer-activity-seg--runs')
    const heads = Array.from(popover()!.querySelectorAll('.composer-activity-pop-head')).map(el => el.textContent)
    expect(heads).toEqual(['Shells'])
    expect(popover()!.querySelector('button')).toBeNull()
    expect(popover()!.textContent).toContain('npm run dev')
    expect(popover()!.textContent).toContain('2m 0s')
    wrapper.unmount()
  })

  it('shows no elapsed time for a row whose start is unknown (a reloaded card)', async () => {
    const wrapper = mountBar({ shells: [SHELL_2] })
    await open(wrapper, '.composer-activity-seg--runs')
    expect(popover()!.querySelector('.composer-activity-pop-age')).toBeNull()
    wrapper.unmount()
  })

  it('ticks: a later now value advances the elapsed time', async () => {
    const wrapper = mountBar({ agents: [AGENT] })
    await open(wrapper, '.composer-activity-seg--agents')
    expect(popover()!.querySelector('.composer-activity-pop-age')?.textContent).toBe('1m 5s')
    await wrapper.setProps({ now: NOW + 10_000 })
    expect(popover()!.querySelector('.composer-activity-pop-age')?.textContent).toBe('1m 15s')
    wrapper.unmount()
  })

  it('closes on Escape and returns focus to its chip', async () => {
    const wrapper = mountBar({ agents: [AGENT] })
    const chip = wrapper.get('.composer-activity-seg--agents').element as HTMLElement
    await open(wrapper, '.composer-activity-seg--agents')
    expect(popover()).not.toBeNull()
    wrapper.get('.composer-activity').element.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flushPromises()
    expect(popover()).toBeNull()
    expect(document.activeElement).toBe(chip)
    wrapper.unmount()
  })

  it('closes when the last running row ends, without moving focus', async () => {
    const wrapper = mountBar({ shells: [SHELL] })
    await open(wrapper, '.composer-activity-seg--runs')
    await wrapper.setProps({ shells: [] })
    await flushPromises()
    expect(popover()).toBeNull()
    expect(wrapper.find('.composer-activity-seg').exists()).toBe(false)
    wrapper.unmount()
  })
})
