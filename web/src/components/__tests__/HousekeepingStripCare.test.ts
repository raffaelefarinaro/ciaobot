// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import HousekeepingStrip from '../HousekeepingStrip.vue'
import { resetHomeNoticeWindows } from '../../composables/useHomeNoticeWindows'
import { setHomeRailBeside } from '../../composables/useHomeRail'
import { api } from '../../lib/api'
import type { CareReport } from '../../lib/careReport'
import { careReports, CARE_WORKSPACE } from '../../lib/careReportFixture'
import { useHousekeepingStore } from '../../stores/housekeeping'
import { useProjectStore } from '../../stores/projects'
import { leadLabels, menuLabels, pick, rows, titles } from './needsYouHelpers'

const routerPush = vi.hoisted(() => vi.fn(async () => {}))
vi.mock('../../router', () => ({ router: { push: routerPush, replace: vi.fn(async () => {}) } }))

enableAutoUnmount(afterEach)

const SCHEDULE = `system-memory-curation@${CARE_WORKSPACE}`

/** Answers the care read with `reports` in turn (the last one repeats) and records run-now posts. */
function serve(...reports: CareReport[]) {
  const queue = [...reports]
  const get = vi.spyOn(api, 'get').mockImplementation(async (path: string) => {
    if (path === `/api/care-report?workspace=${CARE_WORKSPACE}`) return (queue.length > 1 ? queue.shift() : queue[0]) as never
    if (path === '/api/schedules') return [] as never
    throw new Error(`unexpected GET ${path}`)
  })
  const post = vi.spyOn(api, 'post').mockImplementation(async (path: string) => {
    if (path === `/api/schedule-run/${SCHEDULE}`) return { schedule_id: SCHEDULE, chat_id: 'care-chat' } as never
    throw new Error(`unexpected POST ${path}`)
  })
  return { get, post }
}

async function mountStrip() {
  const wrapper = mount(HousekeepingStrip, { attachTo: document.body })
  await flushPromises()
  return wrapper
}

describe('HousekeepingStrip — Workspace care', () => {
  beforeEach(() => {
    resetHomeNoticeWindows()
    setHomeRailBeside(false)
    setActivePinia(createPinia())
    useProjectStore().activeWorkspace = CARE_WORKSPACE
    const housekeeping = useHousekeepingStore()
    vi.spyOn(housekeeping, 'init').mockImplementation(() => {})
    housekeeping.actions = []
    routerPush.mockClear()
  })

  afterEach(() => {
    document.body.innerHTML = ''
    vi.restoreAllMocks()
  })

  it.each([
    ['needsYou', 'Care needs you', '2 things to look at: new line in Unsure, memory guide at 91%.', 'Needs attention:'],
    ['blocked', 'Care did not finish', '4 of 6 steps done. Blocked: hygiene, guide-review.', 'Error:'],
  ] as const)('raises one row for %s, with Open report and the care menu', async (state, title, line, spoken) => {
    serve(careReports[state])
    const wrapper = await mountStrip()
    expect(titles(wrapper)).toEqual([title])
    const [row] = rows(wrapper)
    expect(row.get('.needs-you-line').text()).toBe(line)
    // The tone is a dot and a spoken word, never colour alone.
    expect(row.find('.needs-you-marker').exists()).toBe(true)
    expect(row.get('.sr-only').text()).toBe(spoken)
    expect(leadLabels(row)).toEqual(['Open report'])
    expect(await menuLabels(row)).toEqual(['Run care again', 'Hide for now'])
  })

  it.each(['needsYou', 'blocked', 'interrupted'] as const)('raises no row for %s while At a glance sits beside the column', async state => {
    setHomeRailBeside(true)
    serve(careReports[state])
    const wrapper = await mountStrip()
    expect(titles(wrapper)).toEqual([])
    expect(wrapper.find('section.needs-you').exists()).toBe(false)
    wrapper.unmount()
  })

  it('follows the rail across the 940px drop live, once in each layout', async () => {
    serve(careReports.blocked)
    const wrapper = await mountStrip()
    expect(titles(wrapper)).toEqual(['Care did not finish'])
    setHomeRailBeside(true)
    await nextTick()
    expect(titles(wrapper)).toEqual([])
    setHomeRailBeside(false)
    await nextTick()
    expect(titles(wrapper)).toEqual(['Care did not finish'])
  })

  it('says a cut-off run in its own words', async () => {
    serve(careReports.interrupted)
    const wrapper = await mountStrip()
    expect(titles(wrapper)[0]).toMatch(/^Care was cut off/)
    expect(rows(wrapper)[0].get('.needs-you-line').text()).toBe('Stopped after 2 of 6 steps. The next run starts fresh.')
  })

  it.each(['complete', 'running', 'never'] as const)('renders no element at all when care is %s', async (state) => {
    serve(careReports[state])
    const wrapper = await mountStrip()
    expect(wrapper.find('.needs-you').exists()).toBe(false)
    expect(wrapper.findAll('*').length).toBe(0)
  })

  it('renders nothing when the care read fails', async () => {
    vi.spyOn(api, 'get').mockRejectedValue(new Error('offline'))
    const wrapper = await mountStrip()
    expect(wrapper.findAll('*').length).toBe(0)
  })

  it('holds the list until the care read settles, so it arrives in one piece', async () => {
    let answer!: (report: CareReport) => void
    vi.spyOn(api, 'get').mockImplementation(() => new Promise(resolve => { answer = resolve as never }))
    useHousekeepingStore().actions = [{
      id: 'a', kind: 'k', severity: 1, title: 'Another notice', detail: 'x', glyph: '', workspace: CARE_WORKSPACE,
      blocking: false, view_label: '', view_route: '', run_label: 'Run', chat_label: '', chat_prompt: '',
    }]
    const wrapper = mount(HousekeepingStrip)
    await nextTick()
    expect(wrapper.find('.needs-you').exists()).toBe(false)
    answer(careReports.needsYou)
    await flushPromises()
    expect(titles(wrapper)).toEqual(['Another notice', 'Care needs you'])
  })

  it('opens the report page from the primary button', async () => {
    serve(careReports.needsYou)
    const wrapper = await mountStrip()
    await rows(wrapper)[0].get('.needs-you-primary button').trigger('click')
    await flushPromises()
    expect(routerPush).toHaveBeenCalledWith('/memory/care')
  })

  it('runs care again through the schedule run-now and then shows nothing while it runs', async () => {
    const { post } = serve(careReports.blocked, careReports.blocked, careReports.running)
    const wrapper = await mountStrip()
    await pick(rows(wrapper)[0], 'Run care again')
    await flushPromises()
    expect(post).toHaveBeenCalledWith(`/api/schedule-run/${SCHEDULE}`)
    expect(rows(wrapper)).toHaveLength(0)
    expect(wrapper.get('.needs-you-status').text()).toBe('Care started.')
  })

  it('does not start a second run when one is already in progress', async () => {
    const { post } = serve(careReports.blocked, careReports.running)
    const wrapper = await mountStrip()
    await pick(rows(wrapper)[0], 'Run care again')
    await flushPromises()
    expect(post).not.toHaveBeenCalled()
    expect(wrapper.get('.needs-you-status').text()).toBe('Care is already running.')
  })

  it('reaches the menu by keyboard and hides the row for now', async () => {
    serve(careReports.needsYou)
    const wrapper = await mountStrip()
    const trigger = wrapper.get('.needs-you-more')
    expect(trigger.attributes('aria-label')).toBe('More actions for Care needs you')
    await trigger.trigger('click')
    await nextTick()
    const items = wrapper.findAll('[role="menuitem"]')
    expect(document.activeElement).toBe(items[0].element)
    await wrapper.get('[role="menu"]').trigger('keydown', { key: 'ArrowDown' })
    expect(document.activeElement).toBe(items[1].element)
    await items[1].trigger('click')
    await flushPromises()
    expect(wrapper.find('.needs-you').exists()).toBe(false)
  })

  it('shows another workspace nothing from this one', async () => {
    serve({ ...careReports.needsYou, workspace: 'storybook-beta' })
    const wrapper = await mountStrip()
    expect(wrapper.find('.needs-you').exists()).toBe(false)
  })
})
