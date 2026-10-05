// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import HomeReviewSummary from '../HomeReviewSummary.vue'
import { useProposalsStore } from '../../stores/proposals'
import { useTaskStore } from '../../stores/tasks'
import { useProjectStore } from '../../stores/projects'
import { useVaultReviewStore } from '../../stores/vaultReview'
import { useTaskSignalsStore } from '../../stores/taskSignals'
import { api } from '../../lib/api'
import type { ProposalRow, Schedule, Task } from '../../lib/types'

const router = {
  push: vi.fn(() => Promise.resolve()),
}

vi.mock('vue-router', () => ({ useRouter: () => router }))
vi.mock('../../lib/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), del: vi.fn() },
}))

const get = vi.mocked(api.get)

function proposal(id: string, workspace: string): ProposalRow {
  return {
    id,
    kind: 'memory',
    text: `Fact for ${workspace}`,
    source: `${workspace}-chat`,
    workspace,
    path: 'MEMORY.md',
    line: 1,
    region: 'memory',
    leak_warning: false,
  } as ProposalRow
}

function schedule(id: string, workspace: string): Schedule {
  return {
    schedule_id: id,
    title: `${workspace} briefing`,
    prompt: 'Write the briefing',
    frequency: 'daily',
    enabled: true,
    workspace,
    next_run: '2026-09-25T08:00:00Z',
  } as Schedule
}

describe('HomeReviewSummary', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    router.push.mockClear()
    get.mockReset()
    get.mockImplementation((url: string) => {
      if (url === '/api/proposals') return Promise.resolve({ rows: [] })
      if (url.includes('/api/vault/review')) {
        return Promise.resolve({ candidates: [], trashed: [], cleared: [] })
      }
      return Promise.resolve({})
    })

    const projects = useProjectStore()
    projects.activeWorkspace = 'personal'
    projects.chats = []
    projects.projects = [
      { project_id: 'personal-general', name: 'General', workspace: 'personal' },
      { project_id: 'work-general', name: 'General', workspace: 'work' },
    ] as unknown as typeof projects.projects

    const proposals = useProposalsStore()
    proposals.loaded = true
    proposals.rows = [proposal('personal-one', 'personal')]

    const tasks = useTaskStore()
    tasks.schedulesLoaded = true
    tasks.schedules = [schedule('personal-briefing', 'personal')]

    const retirement = useVaultReviewStore()
    retirement.loadedWorkspace = 'personal'
  })

  it('counts only the active workspace and refreshes on scope changes', async () => {
    const wrapper = mount(HomeReviewSummary)
    expect(wrapper.get('#home-review-title').text()).toBe('At a glance')
    // Up-to-date queues drop out; only what needs a look is listed.
    let titles = wrapper.findAll('.home-review-title').map(node => node.text())
    expect(titles).toEqual(['1 memory proposal', '1 active automation'])

    const projects = useProjectStore()
    const proposals = useProposalsStore()
    const tasks = useTaskStore()
    proposals.rows.push(
      proposal('work-one', 'work'),
      proposal('work-two', 'work'),
    )
    tasks.schedules.push(
      schedule('work-one', 'work'),
      schedule('work-two', 'work'),
    )
    projects.activeWorkspace = 'work'
    await nextTick()
    await flushPromises()

    titles = wrapper.findAll('.home-review-title').map(node => node.text())
    expect(titles).toContain('2 memory proposals')
    expect(titles).toContain('2 active automations')
    wrapper.unmount()
  })

  it('says so when nothing needs review', () => {
    useProposalsStore().rows = []
    useTaskStore().schedules = []
    const wrapper = mount(HomeReviewSummary)
    expect(wrapper.findAll('.home-review-item')).toHaveLength(0)
    expect(wrapper.get('.home-review-clear').text()).toBe('Nothing to review.')
    wrapper.unmount()
  })

  it('initializes the retirement queue for a fresh home visit', async () => {
    const retirement = useVaultReviewStore()
    retirement.loadedWorkspace = null
    get.mockImplementation((url: string) => {
      if (url === '/api/proposals') return Promise.resolve({ rows: [] })
      if (url.includes('/api/vault/review')) {
        return Promise.resolve({
          candidates: [{ id: 'note-1', signals: ['unlinked'] }],
          trashed: [],
          cleared: [],
        })
      }
      return Promise.resolve({})
    })

    const wrapper = mount(HomeReviewSummary)
    await flushPromises()

    expect(get).toHaveBeenCalledWith(expect.stringContaining('workspace=personal'))
    expect(wrapper.findAll('.home-review-title').map(node => node.text())).toContain('1 note to revisit')
    wrapper.unmount()
  })

  it('marks retained rows as stale and shows loading before a snapshot exists', async () => {
    const proposals = useProposalsStore()
    const tasks = useTaskStore()
    const retirement = useVaultReviewStore()
    proposals.loadError = 'proposal refresh failed'
    tasks.scheduleLoadError = 'schedule refresh failed'
    retirement.loadError = 'retirement refresh failed'

    const stale = mount(HomeReviewSummary)
    expect(stale.text()).toContain('Showing the last successful load')
    expect(stale.findAll('.home-review-item--stale')).toHaveLength(2)
    expect(stale.findAll('.home-review-item--attention')).toHaveLength(1)
    stale.unmount()

    proposals.loaded = false
    proposals.rows = []
    proposals.loadError = ''
    tasks.schedulesLoaded = false
    tasks.schedules = []
    tasks.scheduleLoadError = ''
    retirement.loadedWorkspace = null
    retirement.loadError = ''
    get.mockReturnValue(new Promise(() => {}))

    const loading = mount(HomeReviewSummary)
    expect(loading.findAll('.home-review-item').map(card => card.text())).toEqual([
      expect.stringContaining('Checking'),
      expect.stringContaining('Checking'),
      expect.stringContaining('Checking'),
    ])
    loading.unmount()
  })

  it('renders rows with real counts and the next automation', async () => {
    const proposals = useProposalsStore()
    proposals.rows.push(proposal('personal-two', 'personal'))

    const wrapper = mount(HomeReviewSummary)
    const rows = wrapper.findAll('.home-review-item')
    expect(rows[0].find('.home-review-title').text()).toBe('2 memory proposals')
    expect(rows[1].find('.home-review-title').text()).toBe('1 active automation')
    expect(rows[1].find('.home-review-detail').text()).toContain('personal briefing · next')
    expect(wrapper.find('.home-review-icon').exists()).toBe(false)

    await rows[1].trigger('click')
    expect(router.push).toHaveBeenCalledWith('/schedules')
    wrapper.unmount()
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
    expect(router.push).toHaveBeenCalledWith({ path: '/tasks', query: { task: 't1' } })

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
