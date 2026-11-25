// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { api } from '../../lib/api'
import CareRunOutcome from '../CareRunOutcome.vue'

vi.mock('../../lib/api', () => ({ api: { get: vi.fn() } }))

const get = vi.mocked(api.get)
const PATH = '/api/schedules/system-memory-curation%40personal/care-run?dispatch_id=d-1'
const KEY = `prv_${'a'.repeat(64)}`

const COUNTS = { planned: 4, completed: 3, deferred: 1, failed: 0 }
const item = (status: string, detail = '', item_key = 'k') => ({ item_key, pass_id: 'p', status, detail, receipt_ids: [] })
const OK = {
  state: 'ok',
  care_run: { run_id: 'r1', ...COUNTS, counts_complete: true, reasons: [] },
  report_available: true,
  job_status: 'ok',
  run_id: 'r1',
  counts: COUNTS,
  outcomes: [item('applied'), item('applied'), item('no_change')],
  weekly: { marker: 'advanced' },
}

const mounted: VueWrapper[] = []

type Props = { scheduleId: string; chatId: string; dispatchId: string; workspace: string }

async function mountOutcome(props: Partial<Props> = {}): Promise<VueWrapper> {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:p(.*)*', component: { template: '<i />' } }] })
  await router.push('/')
  const wrapper = mount(CareRunOutcome, {
    props: { scheduleId: 'system-memory-curation@personal', chatId: 'chat-1', dispatchId: 'd-1', workspace: 'personal', ...props },
    global: { plugins: [router] },
    attachTo: document.body,
  })
  mounted.push(wrapper)
  return wrapper
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((res) => { resolve = res })
  return { promise, resolve }
}

beforeEach(() => { get.mockReset() })
afterEach(() => {
  while (mounted.length) mounted.pop()?.unmount()
  document.body.innerHTML = ''
})

describe('CareRunOutcome', () => {
  it('asks for the exact dispatch and shows a loading state first', async () => {
    const pending = deferred<unknown>()
    get.mockReturnValueOnce(pending.promise)
    const wrapper = await mountOutcome()
    expect(wrapper.find('[role="status"]').text()).toContain('Checking the outcome')
    expect(get).toHaveBeenCalledWith(PATH)
    pending.resolve(OK)
    await flushPromises()
    expect(wrapper.find('[role="status"]').exists()).toBe(false)
  })

  it('shows counts, settled tally, weekly state and the run chat link for an ok run', async () => {
    get.mockResolvedValueOnce(OK)
    const wrapper = await mountOutcome()
    await flushPromises()
    expect(wrapper.find('.care-run-counts').text()).toBe('Planned 4 · completed 3 · deferred 1 · failed 0')
    expect(wrapper.find('.care-run-settled').text()).toBe('applied 2 · no change needed 1')
    expect(wrapper.find('.care-run-weekly').text()).toContain('marker advanced')
    expect(wrapper.find('.care-run-incomplete').exists()).toBe(false)
    expect(wrapper.find('.care-run-failed').exists()).toBe(false)
    expect(wrapper.findAll('a').find(a => a.text() === 'Open the run chat')?.attributes('href')).toBe('/chat/chat-1')
  })

  it('groups outstanding items by reason with their reason text, failures first', async () => {
    get.mockResolvedValueOnce({
      ...OK,
      outcomes: [
        item('queued', 'waiting for you', 'q1'),
        item('failed', 'write refused', 'f1'),
        item('deferred_budget', 'over the budget', 'd1'),
        item('blocked', 'locked note', 'b1'),
        item('unfinished', 'run ended first', 'u1'),
        item('conflict', 'two receipts disagree', 'c1'),
        item('incomplete', 'no receipt', 'i1'),
      ],
    })
    const wrapper = await mountOutcome()
    await flushPromises()
    const titles = wrapper.findAll('.care-run-group-title').map(t => t.text())
    expect(titles).toEqual([
      'Failed (1)', 'Conflict (1)', 'Incomplete (1)', 'Blocked (1)', 'Unfinished (1)', 'Deferred by budget (1)', 'Queued for review (1)',
    ])
    expect(wrapper.find('[data-status="failed"] li').text()).toContain('write refused')
    expect(wrapper.find('[data-status="blocked"] .care-run-key').text()).toBe('b1')
    expect(wrapper.find('.care-run-noop').exists()).toBe(false)
  })

  it('says so when an outstanding item recorded no reason', async () => {
    get.mockResolvedValueOnce({ ...OK, outcomes: [item('blocked', '')] })
    const wrapper = await mountOutcome()
    await flushPromises()
    expect(wrapper.find('[data-status="blocked"] li').text()).toContain('No reason recorded.')
  })

  it('links only exact review questions, scoped to the run workspace', async () => {
    get.mockResolvedValueOnce({ ...OK, outcomes: [item('queued', 'ask', KEY), item('queued', 'ask', 'Notes/free-text')] })
    const wrapper = await mountOutcome({ workspace: 'work space' })
    await flushPromises()
    const links = wrapper.findAll('a').filter(a => a.text() === 'Open in Memory Review')
    expect(links).toHaveLength(1)
    expect(links[0]!.attributes('href')).toBe(`/memory/review?show=suggested&workspace=work%20space&proposal=${KEY}`)
  })

  it('offers no review link without a workspace', async () => {
    get.mockResolvedValueOnce({ ...OK, outcomes: [item('queued', 'ask', KEY)] })
    const wrapper = await mountOutcome({ workspace: '' })
    await flushPromises()
    expect(wrapper.findAll('a').some(a => a.text() === 'Open in Memory Review')).toBe(false)
  })

  it('names the weekly marker that did not advance', async () => {
    get.mockResolvedValueOnce({ ...OK, weekly: { marker: 'not_advanced' } })
    const wrapper = await mountOutcome()
    await flushPromises()
    expect(wrapper.find('.care-run-weekly').text()).toContain('not finished')
  })

  it('shows no-op as no change needed, never as success wording', async () => {
    get.mockResolvedValueOnce({ ...OK, counts: { planned: 0, completed: 0, deferred: 0, failed: 0 }, outcomes: [item('no_change'), item('no_change')] })
    const wrapper = await mountOutcome()
    await flushPromises()
    expect(wrapper.find('.care-run-noop').text()).toBe('No change needed.')
    expect(wrapper.text()).not.toMatch(/success|succeeded/i)
  })

  it('shows a failed job distinctly and keeps the counts', async () => {
    get.mockResolvedValueOnce({ ...OK, job_status: 'error', counts: { ...COUNTS, failed: 2 }, outcomes: [item('failed', 'x')] })
    const wrapper = await mountOutcome()
    await flushPromises()
    expect(wrapper.find('.care-run-failed').text()).toBe('This run failed.')
    expect(wrapper.find('.care-run-noop').exists()).toBe(false)
    expect(wrapper.find('.care-run-counts').text()).toContain('failed 2')
  })

  it('does not call an ok job status a success when nothing else says so', async () => {
    get.mockResolvedValueOnce({ ...OK, outcomes: undefined, report_available: false, weekly: undefined })
    const wrapper = await mountOutcome()
    await flushPromises()
    expect(wrapper.text()).toContain('Per-item detail was not recorded')
    expect(wrapper.text()).not.toMatch(/success|succeeded|all done/i)
    expect(wrapper.find('.care-run-noop').exists()).toBe(false)
  })

  it('says a skipped run was skipped', async () => {
    get.mockResolvedValueOnce({ ...OK, job_status: 'skipped', outcomes: [] })
    const wrapper = await mountOutcome()
    await flushPromises()
    expect(wrapper.text()).toContain('This run was skipped.')
  })

  it('says plainly when the counts are incomplete and lists the engine notes', async () => {
    get.mockResolvedValueOnce({
      ...OK,
      care_run: { run_id: 'r1', ...COUNTS, failed: 2, counts_complete: false, reasons: ['2 accept(s) failed'] },
    })
    const wrapper = await mountOutcome()
    await flushPromises()
    expect(wrapper.find('.care-run-incomplete').text()).toContain('Incomplete')
    expect(wrapper.findAll('.care-run-reasons li').map(li => li.text())).toEqual(['2 accept(s) failed'])
  })

  it.each([
    ['pending', 'still running, or its outcome is not recorded yet'],
    ['superseded', 'A newer run replaced this one'],
    ['ambiguous', 'records for this run disagree'],
    ['unavailable', 'cannot be read right now'],
  ])('shows the %s state in words, with no counts', async (state, words) => {
    get.mockResolvedValueOnce({ state, care_run: null, report_available: false })
    const wrapper = await mountOutcome()
    await flushPromises()
    expect(wrapper.text()).toContain(words)
    expect(wrapper.find('.care-run-counts').exists()).toBe(false)
  })

  it('lets an unavailable record be retried', async () => {
    get.mockResolvedValueOnce({ state: 'unavailable', care_run: null, report_available: false })
    const wrapper = await mountOutcome()
    await flushPromises()
    get.mockResolvedValueOnce(OK)
    await wrapper.find('.care-run-retry').trigger('click')
    await flushPromises()
    expect(wrapper.find('.care-run-counts').exists()).toBe(true)
  })

  it('shows an alert with a Retry that asks again and then shows the outcome', async () => {
    get.mockRejectedValueOnce(new Error('offline'))
    const wrapper = await mountOutcome()
    await flushPromises()
    expect(wrapper.find('[role="alert"]').text()).toContain('Could not load the outcome')
    get.mockResolvedValueOnce(OK)
    await wrapper.find('.care-run-retry').trigger('click')
    await flushPromises()
    expect(get).toHaveBeenCalledTimes(2)
    expect(wrapper.find('.care-run-counts').text()).toContain('Planned 4')
  })

  it('treats a malformed answer as an error, not as an outcome', async () => {
    get.mockResolvedValueOnce({ state: 'ok', report_available: true, job_status: 'ok' })
    const wrapper = await mountOutcome()
    await flushPromises()
    expect(wrapper.find('[role="alert"]').exists()).toBe(true)
  })

  it('asks again when the dispatch changes and drops the slow answer for the old one', async () => {
    const first = deferred<unknown>()
    get.mockReturnValueOnce(first.promise)
    const wrapper = await mountOutcome({ dispatchId: 'd-1' })
    get.mockResolvedValueOnce({ ...OK, counts: { ...COUNTS, planned: 9 } })
    await wrapper.setProps({ dispatchId: 'd-2' })
    await flushPromises()
    expect(get).toHaveBeenLastCalledWith('/api/schedules/system-memory-curation%40personal/care-run?dispatch_id=d-2')
    expect(wrapper.find('.care-run-counts').text()).toContain('Planned 9')
    first.resolve(OK)
    await flushPromises()
    expect(wrapper.find('.care-run-counts').text()).toContain('Planned 9')
  })

  it('asks again for the new schedule when the schedule changes', async () => {
    get.mockResolvedValueOnce(OK)
    const wrapper = await mountOutcome()
    await flushPromises()
    get.mockResolvedValueOnce({ ...OK, counts: { ...COUNTS, planned: 7 } })
    await wrapper.setProps({ scheduleId: 'system-memory-curation@work' })
    await flushPromises()
    expect(get).toHaveBeenLastCalledWith('/api/schedules/system-memory-curation%40work/care-run?dispatch_id=d-1')
    expect(wrapper.find('.care-run-counts').text()).toContain('Planned 7')
  })

  it('drops a slow first answer when the schedule changes', async () => {
    const first = deferred<unknown>()
    get.mockReturnValueOnce(first.promise)
    const wrapper = await mountOutcome({ scheduleId: 'system-memory-curation@personal' })
    get.mockResolvedValueOnce({ ...OK, counts: { ...COUNTS, planned: 7 } })
    await wrapper.setProps({ scheduleId: 'system-memory-curation@work' })
    await flushPromises()
    first.resolve(OK)
    await flushPromises()
    expect(wrapper.find('.care-run-counts').text()).toContain('Planned 7')
  })

  it('drops a slow first answer when the chat changes', async () => {
    const first = deferred<unknown>()
    get.mockReturnValueOnce(first.promise)
    const wrapper = await mountOutcome({ chatId: 'chat-1' })
    get.mockResolvedValueOnce({ ...OK, counts: { ...COUNTS, planned: 8 } })
    await wrapper.setProps({ chatId: 'chat-2' })
    await flushPromises()
    first.resolve(OK)
    await flushPromises()
    expect(wrapper.find('.care-run-counts').text()).toContain('Planned 8')
  })

  it('renders hostile text from the engine as literal text', async () => {
    const evil = '<img src=x onerror=alert(1)><script>alert(2)</script>'
    get.mockResolvedValueOnce({
      ...OK,
      care_run: { run_id: 'r1', ...COUNTS, counts_complete: false, reasons: [evil] },
      outcomes: [item('failed', evil, evil)],
    })
    const wrapper = await mountOutcome()
    await flushPromises()
    expect(wrapper.find('img').exists()).toBe(false)
    expect(wrapper.find('script').exists()).toBe(false)
    expect(wrapper.find('.care-run-detail').text()).toBe(evil)
    expect(wrapper.find('.care-run-key').text()).toBe(evil)
    expect(wrapper.find('.care-run-reasons li').text()).toBe(evil)
  })

  it('keeps Retry and links as native, focusable controls', async () => {
    get.mockRejectedValueOnce(new Error('offline'))
    const wrapper = await mountOutcome()
    await flushPromises()
    const retry = wrapper.find('.care-run-retry').element as HTMLButtonElement
    expect(retry.tagName).toBe('BUTTON')
    expect(retry.type).toBe('button')
    retry.focus()
    expect(document.activeElement).toBe(retry)
    get.mockResolvedValueOnce({ ...OK, outcomes: [item('queued', 'ask', KEY)] })
    await wrapper.find('.care-run-retry').trigger('click')
    await flushPromises()
    expect(wrapper.find('ul.care-run-items').exists()).toBe(true)
    for (const a of wrapper.findAll('a')) expect(a.element.tagName).toBe('A')
  })
})
