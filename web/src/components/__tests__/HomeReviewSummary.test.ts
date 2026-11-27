// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import HomeReviewSummary from '../HomeReviewSummary.vue'
import { useTaskStore } from '../../stores/tasks'
import { useProjectStore } from '../../stores/projects'
import { useTaskSignalsStore } from '../../stores/taskSignals'
import { api } from '../../lib/api'
import type { Schedule, Task } from '../../lib/types'
import type { CareReport } from '../../lib/careReport'
import { careReports } from '../../lib/careReportFixture'

const router = {
  push: vi.fn(() => Promise.resolve()),
}

vi.mock('vue-router', () => ({ useRouter: () => router }))
vi.mock('../../lib/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), del: vi.fn() },
}))
vi.mock('../../lib/usageStats', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../lib/usageStats')>()),
  fetchUsageStats: vi.fn(() => new Promise(() => {})),
}))

const get = vi.mocked(api.get)

function schedule(id: string, workspace: string, overrides: Partial<Schedule> = {}): Schedule {
  return {
    schedule_id: id,
    title: `${workspace} briefing`,
    prompt: 'Write the briefing',
    frequency: 'daily',
    enabled: true,
    workspace,
    next_run: '2026-09-25T08:00:00Z',
    ...overrides,
  } as Schedule
}

describe('HomeReviewSummary', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    router.push.mockClear()
    get.mockReset()
    get.mockImplementation(() => Promise.resolve({}))

    const projects = useProjectStore()
    projects.activeWorkspace = 'personal'
    projects.chats = []
    projects.projects = [
      { project_id: 'personal-general', name: 'General', workspace: 'personal' },
      { project_id: 'work-general', name: 'General', workspace: 'work' },
    ] as unknown as typeof projects.projects

    const tasks = useTaskStore()
    tasks.schedulesLoaded = true
    tasks.schedules = [schedule('personal-briefing', 'personal')]
  })

  it('counts only the active workspace and refreshes on scope changes', async () => {
    const wrapper = mount(HomeReviewSummary)
    expect(wrapper.get('#home-review-title').text()).toBe('At a glance')
    // A healthy workspace drops out; only what needs a look is listed.
    let titles = wrapper.findAll('.home-review-title').map(node => node.text())
    expect(titles).toEqual([])

    const projects = useProjectStore()
    const tasks = useTaskStore()
    tasks.schedules.push(
      schedule('work-one', 'work', { last_status: 'error' }),
      schedule('work-two', 'work', { last_status: 'error' }),
    )
    projects.activeWorkspace = 'work'
    await nextTick()
    await flushPromises()

    titles = wrapper.findAll('.home-review-title').map(node => node.text())
    expect(titles).toContain('2 automations need a look')

    projects.activeWorkspace = 'personal'
    await nextTick()
    titles = wrapper.findAll('.home-review-title').map(node => node.text())
    expect(titles.some(title => /automation/.test(title))).toBe(false)
    wrapper.unmount()
  })

  it('says so when nothing needs review', () => {
    useTaskStore().schedules = []
    const wrapper = mount(HomeReviewSummary)
    expect(wrapper.findAll('.home-review-item')).toHaveLength(0)
    expect(wrapper.get('.home-review-clear').text()).toBe('Nothing to review.')
    wrapper.unmount()
  })

  it('is one window: a title bar, then the queues, then Today even when nothing needs review', () => {
    useTaskStore().schedules = []
    const wrapper = mount(HomeReviewSummary)
    const root = wrapper.get('.home-review-summary')
    expect(root.classes()[0]).toBe('home-review-summary')
    const title = root.get('.home-review-head #home-review-title')
    expect(title.text()).toBe('At a glance')
    expect(title.classes()).toContain('rail-title')
    expect(root.find('.home-review-head button').exists()).toBe(false)
    expect(root.get('.home-review-clear').text()).toBe('Nothing to review.')
    expect(root.find('.home-today').exists()).toBe(true)
    const html = wrapper.html()
    expect(html.indexOf('home-review-clear')).toBeLessThan(html.indexOf('home-today'))
    wrapper.unmount()
  })

  it('marks retained rows as stale and shows loading before a snapshot exists', async () => {
    const tasks = useTaskStore()
    tasks.scheduleLoadError = 'schedule refresh failed'

    const stale = mount(HomeReviewSummary)
    expect(stale.text()).toContain('Showing the last successful load')
    expect(stale.findAll('.home-review-item--stale')).toHaveLength(1)
    stale.unmount()

    tasks.schedulesLoaded = false
    tasks.schedules = []
    tasks.scheduleLoadError = ''
    get.mockReturnValue(new Promise(() => {}))

    const loading = mount(HomeReviewSummary)
    expect(loading.findAll('.home-review-item').map(card => card.text())).toEqual([
      expect.stringContaining('Checking'),
    ])
    loading.unmount()
  })

  it('makes no requests of its own beyond the care report', async () => {
    const wrapper = mount(HomeReviewSummary)
    await flushPromises()
    expect(get.mock.calls.map(call => call[0])).toEqual(['/api/care-report?workspace=personal'])
    wrapper.unmount()
  })


  describe('workspace care', () => {
    function serveCare(report: CareReport) {
      get.mockImplementation((path: string) => Promise.resolve(
        path.startsWith('/api/care-report') ? { ...report, workspace: 'personal' } : {},
      ) as never)
    }

    it.each([
      ['complete', 'done', /^Done .* · 5 of 5 steps$/],
      ['needsYou', 'done', /^Done .* · 5 of 5 steps$/],
      ['blocked', 'blocked', /^4 of 6 steps · 2 blocked$/],
      ['interrupted', 'blocked', /^Interrupted · 2 of 6 steps$/],
      ['running', 'running', /^Running now · step 3 of 6: learnings$/],
      ['never', 'open', /^Not run yet\. It runs every night\.$/],
    ] as const)('shows one quiet line for %s with its glyph and words', async (state, glyph, detail) => {
      serveCare(careReports[state])
      useTaskStore().schedules = []
      const wrapper = mount(HomeReviewSummary)
      await flushPromises()
      const line = wrapper.get('.home-review-item--care')
      expect(line.get('.home-review-care-title').text()).toBe('Workspace care')
      expect(line.find(`.care-glyph--${glyph}`).exists()).toBe(true)
      expect(line.get('.home-review-detail').text()).toMatch(detail)
      // Quiet: never the warning colour a failing automation takes.
      expect(line.classes()).not.toContain('home-review-item--attention')
      // The care line stands in for "Nothing to review.".
      expect(wrapper.find('.home-review-clear').exists()).toBe(false)
      wrapper.unmount()
    })

    it('opens Memory → Workspace care', async () => {
      serveCare(careReports.blocked)
      const wrapper = mount(HomeReviewSummary)
      await flushPromises()
      await wrapper.get('.home-review-item--care').trigger('click')
      expect(router.push).toHaveBeenCalledWith('/memory/care')
      wrapper.unmount()
    })

    it('shows no care line before the report is read', async () => {
      get.mockImplementation(() => new Promise(() => {}))
      useTaskStore().schedules = []
      const wrapper = mount(HomeReviewSummary)
      await flushPromises()
      expect(wrapper.find('.home-review-item--care').exists()).toBe(false)
      expect(wrapper.text()).toContain('Nothing to review.')
      wrapper.unmount()
    })
  })

  describe('automations', () => {
    function mountWith(schedules: Schedule[]) {
      useTaskStore().schedules = schedules
      return mount(HomeReviewSummary)
    }

    it('flags one failed automation by name and opens it', async () => {
      const wrapper = mountWith([schedule('personal-briefing', 'personal', { last_status: 'error' })])
      const rows = wrapper.findAll('.home-review-item')
      expect(rows).toHaveLength(1)
      expect(rows[0].get('.home-review-title').text()).toBe('1 automation needs a look')
      expect(rows[0].get('.home-review-detail').text()).toBe('personal briefing · the last run failed.')
      expect(rows[0].classes()).toContain('home-review-item--attention')
      await rows[0].trigger('click')
      expect(router.push).toHaveBeenCalledWith('/schedules/personal-briefing')
      wrapper.unmount()
    })

    it('flags an automation whose chat is gone even though it is turned off', () => {
      const wrapper = mountWith([
        schedule('personal-briefing', 'personal', { enabled: false, last_status: 'missing-chat' }),
      ])
      expect(wrapper.get('.home-review-detail').text()).toBe(
        'personal briefing · its chat was deleted, so it is turned off.',
      )
      wrapper.unmount()
    })

    it('says a missed run in words', () => {
      const wrapper = mountWith([schedule('personal-briefing', 'personal', { missed: true })])
      expect(wrapper.get('.home-review-detail').text().endsWith('it did not run when expected.')).toBe(true)
      wrapper.unmount()
    })

    it('groups several failures and opens the list', async () => {
      const wrapper = mountWith([
        schedule('a', 'personal', { last_status: 'error' }),
        schedule('b', 'personal', { missed: true }),
      ])
      const row = wrapper.get('.home-review-item')
      expect(row.get('.home-review-title').text()).toBe('2 automations need a look')
      expect(row.get('.home-review-detail').text()).toBe(
        'Some automations failed or did not run. Open Automations to check them.',
      )
      await row.trigger('click')
      expect(router.push).toHaveBeenCalledWith('/schedules')
      wrapper.unmount()
    })

    it('leaves healthy, disabled, other-workspace and missed one-time automations out', () => {
      const wrapper = mountWith([
        schedule('healthy', 'personal'),
        schedule('off', 'personal', { enabled: false, last_status: 'error' }),
        schedule('other', 'personal', { workspace: 'work', last_status: 'error' }),
        schedule('once', 'personal', { frequency: 'once', missed: true }),
      ])
      expect(wrapper.findAll('.home-review-item')).toHaveLength(0)
      expect(wrapper.get('.home-review-clear').text()).toBe('Nothing to review.')
      wrapper.unmount()
    })

    it('shows a disabled one-time reminder whose chat is gone', () => {
      // The backend's missed-schedules notice covers only enabled reminders.
      const wrapper = mountWith([
        schedule('once', 'personal', {
          frequency: 'once', enabled: false, missed: true, last_status: 'missing-chat',
        }),
      ])
      expect(wrapper.get('.home-review-title').text()).toBe('1 automation needs a look')
      wrapper.unmount()
    })

    it('names an untitled automation by its prompt', () => {
      const wrapper = mountWith([
        schedule('x', 'personal', { title: '', prompt: 'Summarise the inbox\nmore', last_status: 'error' }),
      ])
      expect(wrapper.get('.home-review-detail').text().startsWith('Summarise the inbox · ')).toBe(true)
      wrapper.unmount()
    })
  })

  it('never links a memory pass from this rail', async () => {
    // The pass is a chat, and Home lists every open pass as one row under
    // *memory insights* (HomeRecentChats) — an entry that opens the pass and
    // says what state it is in. A shortcut to "the newest one" here was a
    // second way into one row out of many, and it was the only place on the
    // surface that could name a pass at all.
    const projects = useProjectStore()
    projects.projects = [
      ...projects.projects,
      { project_id: 'personal-memory', name: 'Memory', workspace: 'personal', kind: 'memory' },
    ] as unknown as typeof projects.projects
    projects.chats = [
      { chat_id: 'pass-1', project_id: 'personal-memory', title: 'Memory pass · earlier', archived: true, last_activity_at: '2026-09-20T10:00:00Z' },
      { chat_id: 'pass-2', project_id: 'personal-memory', title: 'Memory pass · current', archived: false, last_activity_at: '2026-09-21T10:00:00Z' },
    ] as unknown as typeof projects.chats

    const switchChat = vi.spyOn(projects, 'switchChat').mockResolvedValue(undefined)
    const wrapper = mount(HomeReviewSummary)
    expect(wrapper.text()).not.toContain('Open Memory')
    expect(wrapper.text()).not.toContain('memory pass')
    expect(switchChat).not.toHaveBeenCalled()
    wrapper.unmount()
  })
  it('lists tasks the agent reported done and opens the one card directly', async () => {
    const signals = useTaskSignalsStore()
    const reviewTask = (id: string, title: string) => ({
      id, title, status: 'in_review', attempt_state: 'ready_for_review', live_attempt_id: `a-${id}`,
    }) as unknown as Task
    signals.loadedWorkspace = 'personal'
    signals.tasks = [
      reviewTask('t1', 'Check the Q4 status'),
      // Moved to In review by hand: no attempt, so nothing for the agent to approve.
      { id: 't2', title: 'Manual', status: 'in_review', attempt_state: '', live_attempt_id: '' } as unknown as Task,
    ]
    const wrapper = mount(HomeReviewSummary)
    const first = wrapper.findAll('.home-review-item')[0]
    expect(first.get('.home-review-title').text()).toBe('1 task in review')
    expect(first.get('.home-review-detail').text()).toContain('Check the Q4 status')
    await first.trigger('click')
    expect(router.push).toHaveBeenCalledWith({ name: 'task-detail', params: { taskId: 't1' } })

    signals.tasks = [reviewTask('t1', 'One'), reviewTask('t3', 'Two')]
    await nextTick()
    const row = wrapper.findAll('.home-review-item')[0]
    expect(row.get('.home-review-title').text()).toBe('2 tasks in review')
    await row.trigger('click')
    expect(router.push).toHaveBeenLastCalledWith('/tasks')

    // Another workspace's snapshot never counts here.
    signals.loadedWorkspace = 'work'
    await nextTick()
    expect(wrapper.text()).not.toContain('in review')
  })
})
