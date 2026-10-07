// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { useProjectStore } from '../../stores/projects'
import BackgroundRunRows from '../BackgroundRunRows.vue'
import type { BackgroundRunSummary } from '../../lib/types'

const START = Date.parse('2026-10-06T10:00:00Z')
const RUN: BackgroundRunSummary = {
  run_id: 'r1', label: '', cmd: ['git', 'clone', 'rizzo-flow'], started_at: '2026-10-06T10:00:00Z', status: 'running', exit_code: null,
}

describe('BackgroundRunRows', () => {
  beforeEach(() => setActivePinia(createPinia()))

  it('names the run by its command and shows how long it has gone', () => {
    const wrapper = mount(BackgroundRunRows, { props: { chatId: 'c1', runs: [RUN], now: START + 65_000 } })
    expect(wrapper.get('.bg-run-label').text()).toBe('git clone rizzo-flow')
    expect(wrapper.get('.bg-run-elapsed').text()).toBe('1m 5s')
  })

  it('reads the log on demand and hides it again', async () => {
    const store = useProjectStore()
    const fetchLog = vi.spyOn(store, 'fetchBackgroundRunLog').mockResolvedValue({ ...RUN, last_lines: ['Cloning…', 'done.'] })
    const wrapper = mount(BackgroundRunRows, { props: { chatId: 'c1', runs: [RUN], now: START } })
    const toggle = wrapper.findAll('.bg-run-action')[0]
    await toggle.trigger('click')
    await flushPromises()
    expect(fetchLog).toHaveBeenCalledWith('c1', 'r1')
    expect(wrapper.get('.bg-run-log pre').text()).toBe('Cloning…\ndone.')
    expect(toggle.attributes('aria-expanded')).toBe('true')
    await toggle.trigger('click')
    expect(wrapper.find('.bg-run-log').exists()).toBe(false)
  })

  it('stops a run and says so when it cannot', async () => {
    const store = useProjectStore()
    const cancel = vi.spyOn(store, 'cancelBackgroundRun').mockRejectedValueOnce(new Error('boom')).mockResolvedValueOnce()
    const wrapper = mount(BackgroundRunRows, { props: { chatId: 'c1', runs: [RUN], now: START } })
    const stop = () => wrapper.get('.bg-run-action--stop')

    await stop().trigger('click')
    await flushPromises()
    expect(wrapper.get('.bg-run-error').text()).toContain('Could not stop')
    expect(stop().attributes('disabled')).toBeUndefined()

    await stop().trigger('click')
    await flushPromises()
    expect(cancel).toHaveBeenCalledTimes(2)
    // Stays "Stopping…" until the finish edge removes the row.
    expect(stop().text()).toBe('Stopping…')
    expect(stop().attributes('disabled')).toBeDefined()
  })
})
