// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import UsageView from '../UsageView.vue'
import { useProjectStore } from '../../stores/projects'
import { api } from '../../lib/api'

vi.mock('../../lib/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), del: vi.fn() },
}))

const get = vi.mocked(api.get)

const SAMPLE = {
  range_days: 30,
  generated_at: '2026-11-09T10:00:00Z',
  totals: { chats_opened: 248, messages_sent: 4517, chats_archived: 171, memories_saved: 112, tasks_completed: 37, projects_worked_on: 16, automation_runs: 180, tokens: 71500000 },
  previous: { chats_opened: 210, messages_sent: 3702, chats_archived: 157, memories_saved: 85, tasks_completed: 31, projects_worked_on: 13, automation_runs: 173, tokens: null },
}

function deferred<T>() {
  let resolve!: (v: T) => void
  let reject!: (e: unknown) => void
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

function mountView() {
  return mount(UsageView, { global: { stubs: { PaneHeader: true } } })
}

beforeEach(() => {
  get.mockReset()
  setActivePinia(createPinia())
  useProjectStore().activeWorkspace = 'personal'
})

describe('UsageView', () => {
  it('shows the eight totals with deltas against the previous period', async () => {
    get.mockResolvedValue(SAMPLE)
    const wrapper = mountView()
    await flushPromises()

    const tiles = wrapper.findAll('.usage-kpi')
    expect(tiles).toHaveLength(8)
    expect(wrapper.findAll('.usage-kpi-label').map(l => l.text())).toEqual([
      'Chats opened', 'Messages sent', 'Chats archived', 'Memories saved',
      'Tasks completed', 'Projects worked on', 'Automation runs', 'Tokens processed',
    ])
    expect(tiles[0].find('.usage-kpi-value').text()).toBe('248')
    expect(tiles[0].find('.usage-kpi-delta').text()).toBe('+18% vs previous 30 days')

    const tokens = tiles[7]
    expect(tokens.find('.usage-kpi-value').text()).toBe('71.5M')
    expect(tokens.find('.usage-kpi-delta').exists()).toBe(false)
  })

  it('shows dashes and no deltas while loading', async () => {
    get.mockReturnValue(new Promise(() => {}))
    const wrapper = mountView()
    await nextTick()

    expect(wrapper.findAll('.usage-kpi-value').every(v => v.text() === '—')).toBe(true)
    expect(wrapper.find('.usage-kpi-delta').exists()).toBe(false)
    expect(wrapper.find('.page-main').attributes('aria-busy')).toBe('true')
    expect(wrapper.find('.spinner, [role="progressbar"]').exists()).toBe(false)
  })

  it('offers four pressed toggles and reloads on a new range', async () => {
    get.mockResolvedValueOnce(SAMPLE)
    const wrapper = mountView()
    await flushPromises()

    const buttons = wrapper.findAll('[role="group"][aria-label="Range"] button')
    expect(buttons.map(b => b.text())).toEqual(['7 days', '30 days', '90 days', 'Year'])
    expect(buttons.map(b => b.attributes('aria-pressed'))).toEqual(['false', 'true', 'false', 'false'])

    const second = deferred<typeof SAMPLE>()
    get.mockReturnValueOnce(second.promise)
    await buttons[2].trigger('click')
    expect(get).toHaveBeenLastCalledWith('/api/stats?workspace=personal&range=90')
    expect(buttons[2].attributes('aria-pressed')).toBe('true')
    expect(buttons[1].attributes('aria-pressed')).toBe('false')
    expect(wrapper.findAll('.usage-kpi-value').every(v => v.text() === '—')).toBe(true)
  })

  it('ignores a response for a range that is no longer selected', async () => {
    const first = deferred<typeof SAMPLE>()
    get.mockReturnValueOnce(first.promise)
    const wrapper = mountView()
    await nextTick()

    get.mockResolvedValueOnce({ ...SAMPLE, totals: { ...SAMPLE.totals, chats_opened: 61 } })
    const buttons = wrapper.findAll('[role="group"][aria-label="Range"] button')
    await buttons[0].trigger('click')
    await flushPromises()

    first.resolve({ ...SAMPLE, totals: { ...SAMPLE.totals, chats_opened: 248 } })
    await flushPromises()

    expect(wrapper.findAll('.usage-kpi-value')[0].text()).toBe('61')
  })

  it('says when the load failed and retries', async () => {
    get.mockRejectedValueOnce(new Error('boom'))
    const wrapper = mountView()
    await flushPromises()

    expect(wrapper.find('.usage-error[role="alert"]').text()).toContain('Could not load usage. boom')
    expect(wrapper.findAll('.usage-kpi-value').every(v => v.text() === '—')).toBe(true)

    get.mockResolvedValueOnce(SAMPLE)
    await wrapper.find('.usage-retry').trigger('click')
    await flushPromises()

    expect(get).toHaveBeenCalledTimes(2)
    expect(wrapper.find('.usage-error').exists()).toBe(false)
    expect(wrapper.findAll('.usage-kpi-value')[0].text()).toBe('248')
  })

  it('reloads when the workspace changes', async () => {
    get.mockResolvedValue(SAMPLE)
    mountView()
    await flushPromises()

    useProjectStore().activeWorkspace = 'work'
    await nextTick()
    await flushPromises()

    expect(get).toHaveBeenLastCalledWith('/api/stats?workspace=work&range=30')
  })

  it('shows the breakdown under the totals', async () => {
    get.mockResolvedValue(SAMPLE)
    const wrapper = mountView()
    await flushPromises()

    const html = wrapper.html()
    expect(wrapper.find('.usage-breakdown').exists()).toBe(true)
    expect(html.indexOf('usage-breakdown')).toBeGreaterThan(html.indexOf('usage-totals'))
  })
})
