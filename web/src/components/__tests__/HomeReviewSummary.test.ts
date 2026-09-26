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
import { api } from '../../lib/api'
import type { ProposalRow, Schedule } from '../../lib/types'

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

    await wrapper.get('.home-review-link').trigger('click')
    expect(router.push).toHaveBeenCalledWith('/memory')

    await rows[1].trigger('click')
    expect(router.push).toHaveBeenCalledWith('/schedules')
    wrapper.unmount()
  })

  it('shows the open memory pass link only when a pass exists', async () => {
    const projects = useProjectStore()
    // The Memory project is hidden from the sidebar, so this rail is the way in
    // to a pass that is queued, running, or waiting on the owner.
    projects.projects = [
      ...projects.projects,
      { project_id: 'personal-memory', name: 'Memory', workspace: 'personal', kind: 'memory' },
    ] as unknown as typeof projects.projects

    const none = mount(HomeReviewSummary)
    expect(none.findAll('.home-review-link').map(l => l.text())).toEqual(['Open Memory'])
    none.unmount()

    projects.chats = [
      { chat_id: 'pass-1', project_id: 'personal-memory', title: 'Memory pass · earlier', archived: true, last_activity_at: '2026-09-20T10:00:00Z' },
      { chat_id: 'pass-2', project_id: 'personal-memory', title: 'Memory pass · current', archived: false, last_activity_at: '2026-09-21T10:00:00Z' },
    ] as unknown as typeof projects.chats

    const some = mount(HomeReviewSummary)
    const links = some.findAll('.home-review-link')
    // Distinct wording from the /memory view link directly above it.
    expect(links.map(l => l.text())).toEqual(['Open Memory', 'Open memory pass'])

    const switchChat = vi.spyOn(projects, 'switchChat').mockResolvedValue(undefined)
    await links[1].trigger('click')
    expect(switchChat).toHaveBeenCalledWith('pass-2')
    // The archived pass is not what the link opens.
    expect(switchChat).not.toHaveBeenCalledWith('pass-1')
    some.unmount()
  })
})
