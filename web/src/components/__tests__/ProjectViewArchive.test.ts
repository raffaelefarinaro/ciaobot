// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'
import { useFileViewerStore } from '../../stores/fileViewer'

const ARCHIVE_PATH = 'memory-vault/Logs/Chats/chat-old/claude/2026-10-01-s.md'

const apiGet = vi.hoisted(() => vi.fn())
vi.mock('../../lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../lib/api')>()),
  api: { get: apiGet, post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() },
}))

vi.mock('../../lib/confirm', () => ({ askConfirm: vi.fn() }))

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}))

function seed() {
  const store = useProjectStore()
  store.projects = [{
    project_id: 'project-1',
    name: 'Upwordo',
    workspace: 'personal',
    is_auto: false,
    repo: null,
  }] as unknown as typeof store.projects
  store.chats = [] as unknown as typeof store.chats
  store.workspaces = [{ name: 'personal', color: 'emerald' }] as unknown as typeof store.workspaces
  store.projectStreaming = {}
  store.backgroundAgents = {}
  store.bootstrapped = true
  const taskStore = useTaskStore()
  taskStore.schedules = [] as unknown as typeof taskStore.schedules
  vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()
  return store
}

async function flush() {
  await nextTick()
  await Promise.resolve()
  await new Promise(resolve => setTimeout(resolve, 0))
  await nextTick()
}

describe('ProjectView archived chats', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.restoreAllMocks()
    apiGet.mockReset()
    apiGet.mockImplementation(async (url: string) => {
      if (url.includes('archived=1')) {
        return {
          total: 1,
          items: [{
            chat_id: 'chat-old',
            title: 'Old chat',
            created_at: '2026-10-01T00:00:00Z',
            archive_path: ARCHIVE_PATH,
          }],
        }
      }
      throw new Error(`unstubbed ${url}`)
    })
  })

  it('opens an archived transcript in the file viewer, not the pinned panel', async () => {
    seed()
    vi.stubGlobal('fetch', vi.fn(async () => new Response('Transcript')))
    const projects = useProjectStore()
    vi.spyOn(projects, 'pinFile').mockResolvedValue(true)
    const fileViewer = useFileViewerStore()
    fileViewer.setDockTarget('project-1')

    const { default: ProjectView } = await import('../ProjectView.vue')
    const wrapper = mount(ProjectView, {
      props: { projectId: 'project-1' },
      global: { stubs: { PaneHeader: { template: '<div><slot name="title" /><slot name="actions" /></div>' } } },
    })
    await flush()

    await wrapper.get('.archive-toggle').trigger('click')
    await flush()
    await wrapper.get('.chat-row.archived').trigger('click')
    await flush()

    expect(projects.pinFile).not.toHaveBeenCalled()
    expect(fileViewer.isOpen).toBe(true)
    expect(fileViewer.path).toBe(ARCHIVE_PATH)
    wrapper.unmount()
  })

  it('keeps the pane alive when the archive page is malformed', async () => {
    seed()
    // A payload without `items`: a stub, or an engine that does not know the
    // `archived=1` branch and answers the bare array instead.
    apiGet.mockImplementation(async (url: string) => {
      if (url.includes('archived=1')) return {} as never
      throw new Error(`unstubbed ${url}`)
    })

    const { default: ProjectView } = await import('../ProjectView.vue')
    const wrapper = mount(ProjectView, {
      props: { projectId: 'project-1' },
      global: { stubs: { PaneHeader: { template: '<div><slot name="title" /><slot name="actions" /></div>' } } },
    })
    await flush()

    await wrapper.get('.archive-toggle').trigger('click')
    await flush()

    // The template reads `archiveData.items.length`, so an unguarded payload
    // throws inside the render and takes the whole project pane with it: the
    // section stays on the loading skeleton, and the error it never reached
    // is never said. The section's own error row is the readable outcome
    // instead (#1710).
    expect(wrapper.find('[aria-label="Loading archived chats"]').exists()).toBe(false)
    expect(wrapper.get('[role="alert"]').text()).toContain('Could not load archived chats.')
    wrapper.unmount()
  })
})
