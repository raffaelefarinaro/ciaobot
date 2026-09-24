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
    let cards = wrapper.findAll('.home-review-item')
    expect(cards[0].text()).toContain('1')
    expect(cards[0].text()).not.toContain('2')
    expect(cards[1].text()).toContain('Up to date')
    expect(cards[2].text()).toContain('1')

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

    cards = wrapper.findAll('.home-review-item')
    expect(cards[0].text()).toContain('2')
    expect(cards[2].text()).toContain('2')
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
    expect(wrapper.findAll('.home-review-item')[1].text()).toContain('1')
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

  it('renders pulse rows with real counts and the next automation', async () => {
    const proposals = useProposalsStore()
    proposals.rows.push(proposal('personal-two', 'personal'))

    const wrapper = mount(HomeReviewSummary)
    const rows = wrapper.findAll('.home-review-item')
    expect(rows[0].find('.home-review-title').text()).toBe('2 memory proposals')
    expect(rows[0].find('.home-review-action').text()).toBe('review')
    expect(rows[1].find('.home-review-title').text()).toBe('Notes to revisit')
    expect(rows[1].text()).toContain('Up to date')
    expect(rows[2].find('.home-review-title').text()).toBe('1 active automation')
    expect(rows[2].find('.home-review-detail').text()).toContain('personal briefing · next')

    await wrapper.get('.home-review-link').trigger('click')
    expect(router.push).toHaveBeenCalledWith('/memory')

    await rows[2].trigger('click')
    expect(router.push).toHaveBeenCalledWith('/schedules')
    wrapper.unmount()
  })
})
