// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { defineComponent } from 'vue'
import MemoryCarePanel from '../MemoryCarePanel.vue'
import { api } from '../../lib/api'
import type { CareReport } from '../../lib/careReport'
import { careReports, CARE_WORKSPACE } from '../../lib/careReportFixture'
import { useFileViewerStore } from '../../stores/fileViewer'
import { useProjectStore } from '../../stores/projects'

const startFileDiscussion = vi.hoisted(() => vi.fn(async () => ({ chat_id: 'fix-chat' })))
vi.mock('../../lib/fileDiscussion', () => ({ startFileDiscussion }))
const fetchWorkspaceGuide = vi.hoisted(() => vi.fn(async () => ({ path: 'storybook-alpha/AGENTS.md', content: '', error: '' })))
vi.mock('../../lib/workspaceGuide', () => ({ fetchWorkspaceGuide }))

enableAutoUnmount(afterEach)

const SCHEDULE = `system-memory-curation@${CARE_WORKSPACE}`

function serve(...reports: CareReport[]) {
  const queue = [...reports]
  vi.spyOn(api, 'get').mockImplementation(async (path: string) => {
    if (path.startsWith('/api/care-report')) return (queue.length > 1 ? queue.shift() : queue[0]) as never
    if (path === '/api/schedules') return [] as never
    throw new Error(`unexpected GET ${path}`)
  })
  return vi.spyOn(api, 'post').mockResolvedValue({ schedule_id: SCHEDULE } as never)
}

async function mountPanel() {
  const Empty = defineComponent({ template: '<div />' })
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:p(.*)*', component: Empty }] })
  const wrapper = mount(MemoryCarePanel, { global: { plugins: [router] }, attachTo: document.body })
  await flushPromises()
  return wrapper
}

describe('MemoryCarePanel', () => {
  let open: ReturnType<typeof vi.fn>

  beforeEach(() => {
    setActivePinia(createPinia())
    useProjectStore().activeWorkspace = CARE_WORKSPACE
    open = vi.fn()
    vi.spyOn(useFileViewerStore(), 'open').mockImplementation(open as never)
    startFileDiscussion.mockClear()
  })

  afterEach(() => {
    document.body.innerHTML = ''
    vi.restoreAllMocks()
  })

  it('shows the summary, the numbered needs and every step with its glyph and word', async () => {
    serve(careReports.needsYou)
    const wrapper = await mountPanel()
    expect(wrapper.get('.mc-summary-title').text()).toBe('Done with 2 things for you')
    expect(wrapper.get('.mc-summary-meta').text()).toBe('Nov 27, 03:12 · 5 of 5 steps ticked · weekly checks done')
    expect(wrapper.findAll('.mc-seg--done')).toHaveLength(5)
    expect(wrapper.findAll('.mc-need-title').map(n => n.text())).toEqual(['New line in Unsure', 'Memory guide at 91%'])
    expect(wrapper.findAll('.mc-need .mc-action').map(b => b.text())).toEqual(['Open Unsure', 'Review guide'])
    const steps = wrapper.findAll('.mc-step')
    expect(steps).toHaveLength(5)
    expect(steps[1].get('.mc-step-name').text()).toBe('Memory regions')
    expect(steps[1].get('.mc-step-line').text()).toBe('Trimmed profile region from 91% to 83%')
    expect(steps[1].get('.mc-step-evidence').text()).toBe('verify: ciao memory status -> pct 83')
    expect(steps[1].get('.mc-step-state').text()).toBe('done')
    expect(steps[1].find('.care-glyph--done').exists()).toBe(true)
    expect(wrapper.get('.mc-foot-note').text()).toBe('Weekly checks last done Nov 27')
  })

  it('opens existing destinations only: Unsure, the guide, the report', async () => {
    serve(careReports.needsYou)
    const wrapper = await mountPanel()
    const [unsure, guide] = wrapper.findAll('.mc-need .mc-action')
    await unsure.trigger('click')
    expect(open).toHaveBeenLastCalledWith('memory-vault/storybook-alpha/Workspace/Unsure.md')
    await guide.trigger('click')
    await flushPromises()
    expect(fetchWorkspaceGuide).toHaveBeenCalledWith(CARE_WORKSPACE)
    expect(open).toHaveBeenLastCalledWith('storybook-alpha/AGENTS.md')
    const report = wrapper.findAll('.mc-link').find(b => b.text() === 'Open Care-Report.md')!
    await report.trigger('click')
    expect(open).toHaveBeenLastCalledWith('memory-vault/storybook-alpha/Workspace/Care-Report.md')
  })

  it('marks blocked steps by shape and word and offers a fix chat that is never sent', async () => {
    serve(careReports.blocked)
    const wrapper = await mountPanel()
    expect(wrapper.get('.mc-summary-title').text()).toBe('Did not finish: 4 of 6 steps')
    expect(wrapper.findAll('.mc-seg--blocked')).toHaveLength(2)
    const hygiene = wrapper.findAll('.mc-step')[4]
    expect(hygiene.get('.mc-step-state').text()).toBe('blocked')
    expect(hygiene.find('.care-glyph--blocked').exists()).toBe(true)
    expect(hygiene.get('.mc-step-line').text()).toBe('Blocked: cannot write INDEX.md')
    const fixButtons = wrapper.findAll('.mc-step .mc-action')
    expect(fixButtons.map(b => b.attributes('aria-label'))).toEqual(['Fix Hygiene with a chat', 'Fix Guide review with a chat'])
    await fixButtons[0].trigger('click')
    await flushPromises()
    expect(startFileDiscussion).toHaveBeenCalledTimes(1)
    const [, options] = startFileDiscussion.mock.calls[0] as unknown as [unknown, { path: string; seed: string; title: string; workspace: string }]
    expect(options.path).toBe('memory-vault/storybook-alpha/Workspace/Care-Report.md')
    expect(options.title).toBe('Fix care step: hygiene')
    expect(options.workspace).toBe(CARE_WORKSPACE)
    expect(options.seed).toContain('Reason: cannot write INDEX.md')
    expect(options.seed).toContain('Step file: /stock/references/hygiene.md')
  })

  it('shows the running step and runs care again only when nothing runs', async () => {
    const post = serve(careReports.running)
    const wrapper = await mountPanel()
    expect(wrapper.get('.mc-summary-title').text()).toBe('Running: step 3 of 6')
    const learnings = wrapper.findAll('.mc-step')[2]
    expect(learnings.find('.care-glyph--running').exists()).toBe(true)
    expect(learnings.get('.mc-step-state').text()).toBe('running')
    const run = wrapper.findAll('.mc-link').find(b => b.text() === 'Run care again')!
    await run.trigger('click')
    await flushPromises()
    expect(post).not.toHaveBeenCalled()
    expect(wrapper.get('.mc-status').text()).toBe('Care is already running.')
  })

  it('runs care again through the schedule run-now', async () => {
    const post = serve(careReports.blocked, careReports.blocked, careReports.blocked, careReports.running)
    const wrapper = await mountPanel()
    const run = wrapper.findAll('.mc-link').find(b => b.text() === 'Run care again')!
    await run.trigger('click')
    await flushPromises()
    expect(post).toHaveBeenCalledWith(`/api/schedule-run/${SCHEDULE}`)
    expect(wrapper.get('.mc-status').text()).toBe('Care started.')
    expect(wrapper.get('.mc-summary-title').text()).toBe('Running: step 3 of 6')
  })

  it('says never run plainly and cannot open a file that does not exist', async () => {
    serve(careReports.never)
    const wrapper = await mountPanel()
    expect(wrapper.get('.mc-summary-title').text()).toBe('Not run yet')
    expect(wrapper.get('.mc-summary-meta').text()).toBe('It runs every night. Its report will show here.')
    expect(wrapper.find('.mc-steps').exists()).toBe(false)
    const report = wrapper.findAll('.mc-link').find(b => b.text() === 'Open Care-Report.md')!
    expect(report.attributes('disabled')).toBeDefined()
    expect(wrapper.get('.mc-foot-note').text()).toBe('Weekly checks not done yet')
  })

  it('links back to Memory and keeps every control a real, focusable button', async () => {
    serve(careReports.blocked)
    const wrapper = await mountPanel()
    expect(wrapper.get('.mc-back').attributes('href')).toBe('/memory/map')
    for (const control of wrapper.findAll('button')) {
      expect(control.attributes('type')).toBe('button')
      ;(control.element as HTMLButtonElement).focus()
      expect(document.activeElement).toBe(control.element)
    }
  })

  it('says when the report cannot be read', async () => {
    vi.spyOn(api, 'get').mockRejectedValue(new Error('engine offline'))
    const wrapper = await mountPanel()
    expect(wrapper.get('[role="alert"]').text()).toBe('Could not read the care report: engine offline')
  })
})
