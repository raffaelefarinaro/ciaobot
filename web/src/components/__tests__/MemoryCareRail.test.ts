// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import MemoryCareRail from '../MemoryCareRail.vue'
import { api } from '../../lib/api'
import type { CareReport } from '../../lib/careReport'
import { careReports, CARE_WORKSPACE, CARE_WRITTEN_AT } from '../../lib/careReportFixture'
import { stampWords } from '../../lib/careReport'
import { useFileViewerStore } from '../../stores/fileViewer'
import { useProjectStore } from '../../stores/projects'
import { useCareReportStore } from '../../stores/careReport'

enableAutoUnmount(afterEach)

const SCHEDULE = `system-memory-curation@${CARE_WORKSPACE}`
const NEXT = new Date(Date.now() + 12 * 3_600_000 + 60_000).toISOString()

function serve(schedules: unknown[], ...reports: CareReport[]) {
  const queue = [...reports]
  vi.spyOn(api, 'get').mockImplementation(async (path: string) => {
    if (path.startsWith('/api/care-report')) return (queue.length > 1 ? queue.shift() : queue[0]) as never
    if (path === '/api/schedules') return schedules as never
    throw new Error(`unexpected GET ${path}`)
  })
  return vi.spyOn(api, 'post').mockResolvedValue({ schedule_id: SCHEDULE } as never)
}

async function mountRail() {
  // The care page loads the report; the rail only reads what the store holds.
  useCareReportStore().show(CARE_WORKSPACE)
  const wrapper = mount(MemoryCareRail, { attachTo: document.body })
  await flushPromises()
  return wrapper
}

const row = { schedule_id: SCHEDULE, next_run: NEXT, enabled: true }

describe('MemoryCareRail', () => {
  let open: ReturnType<typeof vi.fn>

  beforeEach(() => {
    setActivePinia(createPinia())
    useProjectStore().activeWorkspace = CARE_WORKSPACE
    open = vi.fn()
    vi.spyOn(useFileViewerStore(), 'open').mockImplementation(open as never)
  })

  afterEach(() => {
    document.body.innerHTML = ''
    vi.restoreAllMocks()
  })

  it('is the Daily care window with the last run, the next run and the weekly date', async () => {
    serve([row], careReports.needsYou)
    const wrapper = await mountRail()
    expect(wrapper.get('h2').text()).toBe('Daily care')
    const facts = Object.fromEntries(wrapper.findAll('.care-rail-fact').map(f => [f.get('dt').text(), f.get('dd').text()]))
    expect(facts['Last run']).toContain('Done')
    expect(facts['Last run']).toContain(stampWords(CARE_WRITTEN_AT))
    expect(facts['Last run']).toMatch(/ago$/)
    expect(facts['Next run']).toBe(`${stampWords(NEXT)} · in 12h`)
    expect(facts['Weekly checks']).toBe('Nov 27')
    expect(wrapper.find('.care-glyph--done').exists()).toBe(true)
    expect(wrapper.findAll('.docked-row').map(b => b.text())).toEqual(['Run care again', 'Open Care-Report.md'])
  })

  it('says Did not finish for a blocked run and Not scheduled for a paused schedule', async () => {
    const paused = { ...careReports.blocked, schedule: { ...careReports.blocked.schedule!, enabled: false } }
    serve([{ ...row, enabled: false }], paused)
    const wrapper = await mountRail()
    const last = wrapper.findAll('.care-rail-fact')[0]
    expect(last.text()).toContain('Did not finish')
    expect(last.find('.care-glyph--blocked').exists()).toBe(true)
    expect(wrapper.findAll('.care-rail-fact')[1].get('dd').text()).toBe('Not scheduled')
  })

  it('dates a report that cannot be read by the schedule\'s last run, else the file time', async () => {
    const unreadable = { ...careReports.never, status: 'unreadable' as const, modified_at: CARE_WRITTEN_AT,
      schedule: { ...careReports.never.schedule!, last_run_at: '2026-11-26T06:00:00+00:00' } }
    serve([row], unreadable)
    const wrapper = await mountRail()
    const text = wrapper.findAll('.care-rail-fact')[0].text()
    expect(text).toContain('Report unreadable')
    expect(text).toContain(stampWords('2026-11-26T06:00:00+00:00'))
  })

  it('falls back to the report file time when an unreadable report has no schedule run', async () => {
    const unreadable = { ...careReports.never, status: 'unreadable' as const, modified_at: CARE_WRITTEN_AT }
    serve([row], unreadable)
    const wrapper = await mountRail()
    expect(wrapper.findAll('.care-rail-fact')[0].text()).toContain(stampWords(CARE_WRITTEN_AT))
  })

  it('re-reads the relative words every minute and stops when unmounted', async () => {
    vi.useFakeTimers()
    try {
      const soon = new Date(Date.now() + 3 * 3_600_000 + 30_000).toISOString()
      serve([{ ...row, next_run: soon }], careReports.needsYou)
      const wrapper = await mountRail()
      expect(wrapper.findAll('.care-rail-fact')[1].text()).toContain('in 3h')
      vi.advanceTimersByTime(60_000 * 31)
      await flushPromises()
      expect(wrapper.findAll('.care-rail-fact')[1].text()).toContain('in 2h')
      wrapper.unmount()
      expect(vi.getTimerCount()).toBe(0)
    } finally {
      vi.useRealTimers()
    }
  })

  it('says Never run and cannot open a report that does not exist', async () => {
    serve([row], careReports.never)
    const wrapper = await mountRail()
    expect(wrapper.findAll('.care-rail-fact')[0].text()).toContain('Never run')
    expect(wrapper.findAll('.care-rail-fact')[2].get('dd').text()).toBe('Not done yet')
    const [run, report] = wrapper.findAll('.docked-row')
    expect(run.text()).toBe('Run care now')
    expect(report.attributes('disabled')).toBeDefined()
  })

  it('shows Running… disabled while a run is in progress, dated by its dispatch', async () => {
    const post = serve([row], careReports.running)
    const wrapper = await mountRail()
    expect(wrapper.findAll('.care-rail-fact')[0].text()).toContain('Running')
    expect(wrapper.find('.care-glyph--running').exists()).toBe(true)
    const run = wrapper.findAll('.docked-row')[0]
    expect(run.text()).toBe('Running…')
    expect(run.attributes('disabled')).toBeDefined()
    await run.trigger('click')
    await flushPromises()
    expect(post).not.toHaveBeenCalled()
  })

  it('runs care again through the schedule run-now and says what the engine did', async () => {
    const post = serve([row], careReports.blocked, careReports.blocked, careReports.running)
    const wrapper = await mountRail()
    await wrapper.findAll('.docked-row')[0].trigger('click')
    await flushPromises()
    expect(post).toHaveBeenCalledWith(`/api/schedule-run/${SCHEDULE}`)
    expect(wrapper.get('.care-rail-status').text()).toBe('Care started.')
  })

  it('says it only asked when the run has not started yet', async () => {
    const post = serve([row], careReports.blocked)
    const wrapper = await mountRail()
    await wrapper.findAll('.docked-row')[0].trigger('click')
    await flushPromises()
    expect(post).toHaveBeenCalledTimes(1)
    expect(wrapper.get('.care-rail-status').text()).toBe('Care was asked to run but has not started yet.')
  })

  it('opens the report in the file viewer', async () => {
    serve([row], careReports.needsYou)
    const wrapper = await mountRail()
    await wrapper.findAll('.docked-row')[1].trigger('click')
    expect(open).toHaveBeenCalledWith('memory-vault/storybook-alpha/Workspace/Care-Report.md')
  })
})
