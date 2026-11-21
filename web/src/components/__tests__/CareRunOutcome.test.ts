// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { api } from '../../lib/api'
import CareRunOutcome from '../CareRunOutcome.vue'

vi.mock('../../lib/api', () => ({ api: { get: vi.fn() } }))

const get = vi.mocked(api.get)

const RUN = {
  run_id: 'host:1:uuid',
  planned: 4,
  completed: 3,
  deferred: 1,
  failed: 0,
  counts_complete: true,
  reasons: [],
}

const mounted: VueWrapper[] = []

function mountOutcome(props: { scheduleId?: string; chatId?: string } = {}): VueWrapper {
  const wrapper = mount(CareRunOutcome, {
    props: { scheduleId: props.scheduleId ?? 'system-memory-curation@personal', chatId: props.chatId ?? 'chat-1' },
    attachTo: document.body,
  })
  mounted.push(wrapper)
  return wrapper
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

beforeEach(() => {
  get.mockReset()
})

afterEach(() => {
  while (mounted.length) mounted.pop()?.unmount()
  document.body.innerHTML = ''
})

describe('CareRunOutcome', () => {
  it('asks the schedule route for its own chat outcome and shows a loading state first', async () => {
    const pending = deferred<{ care_run: typeof RUN | null }>()
    get.mockReturnValueOnce(pending.promise)
    const wrapper = mountOutcome()
    expect(wrapper.find('[role="status"]').text()).toContain('Checking the outcome')
    expect(get).toHaveBeenCalledWith('/api/schedules/system-memory-curation%40personal/care-run')
    pending.resolve({ care_run: RUN })
    await flushPromises()
    expect(wrapper.find('[role="status"]').exists()).toBe(false)
  })

  it('shows the exact derived counts when the run is complete', async () => {
    get.mockResolvedValueOnce({ care_run: RUN })
    const wrapper = mountOutcome()
    await flushPromises()
    expect(wrapper.find('.care-run-counts').text()).toBe('Planned 4 · completed 3 · deferred 1 · failed 0')
    expect(wrapper.find('.care-run-incomplete').exists()).toBe(false)
  })

  it('says plainly when the counts are incomplete and names the failures from the reasons', async () => {
    get.mockResolvedValueOnce({
      care_run: { ...RUN, failed: 2, counts_complete: false, reasons: ['2 accept(s) failed; the run\'s receipts say which'] },
    })
    const wrapper = mountOutcome()
    await flushPromises()
    expect(wrapper.find('.care-run-counts').text()).toContain('failed 2')
    expect(wrapper.find('.care-run-incomplete').text()).toContain('Incomplete')
    const reasons = wrapper.findAll('.care-run-reasons li').map(item => item.text())
    expect(reasons).toEqual(['2 accept(s) failed; the run\'s receipts say which'])
  })

  it('says no outcome is recorded when the route answers null, without claiming success', async () => {
    get.mockResolvedValueOnce({ care_run: null })
    const wrapper = mountOutcome()
    await flushPromises()
    expect(wrapper.text()).toContain('No outcome recorded for this run yet')
    expect(wrapper.find('.care-run-counts').exists()).toBe(false)
  })

  it('shows an alert with a Retry that asks again and then shows the outcome', async () => {
    get.mockRejectedValueOnce(new Error('offline'))
    const wrapper = mountOutcome()
    await flushPromises()
    expect(wrapper.find('[role="alert"]').text()).toContain('Could not load the outcome')
    expect(wrapper.find('.care-run-counts').exists()).toBe(false)

    get.mockResolvedValueOnce({ care_run: RUN })
    await wrapper.find('.care-run-retry').trigger('click')
    await flushPromises()
    expect(get).toHaveBeenCalledTimes(2)
    expect(wrapper.find('.care-run-counts').text()).toContain('Planned 4')
  })

  it('asks again for the new schedule when the schedule changes', async () => {
    get.mockResolvedValueOnce({ care_run: RUN })
    const wrapper = mountOutcome({ scheduleId: 'system-memory-curation@personal' })
    await flushPromises()
    get.mockResolvedValueOnce({ care_run: { ...RUN, planned: 7 } })
    await wrapper.setProps({ scheduleId: 'system-memory-curation@work' })
    await flushPromises()
    expect(get).toHaveBeenLastCalledWith('/api/schedules/system-memory-curation%40work/care-run')
    expect(wrapper.find('.care-run-counts').text()).toContain('Planned 7')
  })

  it('drops a slow answer for a previous chat when the chat changes', async () => {
    const first = deferred<{ care_run: typeof RUN | null }>()
    get.mockReturnValueOnce(first.promise)
    const wrapper = mountOutcome({ chatId: 'chat-1' })

    get.mockResolvedValueOnce({ care_run: { ...RUN, planned: 9 } })
    await wrapper.setProps({ chatId: 'chat-2' })
    await flushPromises()
    expect(wrapper.find('.care-run-counts').text()).toContain('Planned 9')

    first.resolve({ care_run: RUN })
    await flushPromises()
    expect(wrapper.find('.care-run-counts').text()).toContain('Planned 9')
  })
})
