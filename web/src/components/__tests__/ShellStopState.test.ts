// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount, RouterLinkStub } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import { api } from '../../lib/api'
import { useProjectStore } from '../../stores/projects'
import type { RunningSubagent } from '../../lib/types'
import ComposerActivity from '../ComposerActivity.vue'
import ShellRows from '../ShellRows.vue'

const NOW = Date.parse('2026-10-06T10:10:00Z')
const SHELL: RunningSubagent = {
  agent_id: 'task:b1', description: 'npm run dev', subagent_type: 'shell',
  started_at: new Date(NOW - 125_000).toISOString(), stoppable: true,
}

// Wires the composer popover and a rail list to the store the way ChatPanel does.
function mountHarness() {
  const store = useProjectStore()
  store.runningSubagents = { c1: [SHELL] } as unknown as typeof store.runningSubagents
  const Harness = defineComponent({
    setup: () => () => [
      h(ComposerActivity, {
        chatId: 'c1', agentCount: 0, agents: store.runningSubagentsFor('c1'), now: NOW,
        stopTask: (id: string) => store.stopBackgroundTask('c1', id),
        stopping: store.stoppingTasks.c1, stopErrors: store.stopTaskErrors.c1,
      }),
      h('div', { id: 'rail' }, [h(ShellRows, {
        variant: 'rail', rows: store.runningSubagentsFor('c1'), now: NOW,
        stopTask: (id: string) => store.stopBackgroundTask('c1', id),
        stopping: store.stoppingTasks.c1, errors: store.stopTaskErrors.c1,
      })]),
    ],
  })
  const wrapper = mount(Harness, { attachTo: document.body, global: { stubs: { RouterLink: RouterLinkStub } } })
  return { store, wrapper }
}

const popoverStop = () => document.querySelector<HTMLButtonElement>('.composer-activity-popover .shell-row-stop')
const railStop = () => document.querySelector<HTMLButtonElement>('#rail .shell-row-stop')

beforeEach(() => {
  setActivePinia(createPinia())
  vi.spyOn(api, 'post').mockResolvedValue({} as never)
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
})
afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
  document.body.innerHTML = ''
})

describe('shell stop state', () => {
  it('survives closing and reopening the popover, and shows in the rail too', async () => {
    const { wrapper } = mountHarness()
    const opener = wrapper.get('.composer-activity-seg--runs')
    await opener.trigger('click')
    await flushPromises()
    popoverStop()!.click()
    await flushPromises()
    expect(popoverStop()!.textContent?.trim()).toBe('Stopping…')
    // The rail is a second view of the same task.
    expect(railStop()!.textContent?.trim()).toBe('Stopping…')
    expect(railStop()!.getAttribute('aria-disabled')).toBe('true')

    await opener.trigger('click')
    await flushPromises()
    expect(document.querySelector('.composer-activity-popover')).toBeNull()
    await opener.trigger('click')
    await flushPromises()
    expect(popoverStop()!.textContent?.trim()).toBe('Stopping…')
    expect(popoverStop()!.getAttribute('aria-disabled')).toBe('true')
    expect(wrapper.get('[role="status"]').text()).toBe('Stopping npm run dev')
    wrapper.unmount()
  })

  it('clears the announcement when the stop lock releases, and shows a failure after a close', async () => {
    const { store, wrapper } = mountHarness()
    await wrapper.get('.composer-activity-seg--runs').trigger('click')
    await flushPromises()
    popoverStop()!.click()
    await flushPromises()
    vi.advanceTimersByTime(15_000)
    await flushPromises()
    expect(wrapper.get('[role="status"]').text()).toBe('Running in the background: npm run dev')

    vi.mocked(api.post).mockRejectedValueOnce(new Error('boom'))
    popoverStop()!.click()
    await wrapper.get('.composer-activity-seg--runs').trigger('click')
    await flushPromises()
    expect(store.stopTaskErrors.c1?.b1).toBeTruthy()
    expect(railStop()!.textContent?.trim()).toBe('Stop')
    expect(document.querySelector('#rail [role="alert"]')?.textContent).toContain('Could not stop')
    wrapper.unmount()
  })
})
