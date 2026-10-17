// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'
import { useFileViewerStore } from '../../stores/fileViewer'

const askConfirm = vi.hoisted(() => vi.fn())
vi.mock('../../lib/confirm', () => ({ askConfirm }))

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}))

function seed(repo: unknown = null) {
  const store = useProjectStore()
  store.projects = [{
    project_id: 'project-1',
    name: 'Upwordo',
    workspace: 'personal',
    is_auto: false,
    repo,
  }] as unknown as typeof store.projects
  store.chats = [
    {
      chat_id: 'chat-read',
      project_id: 'project-1',
      title: 'Read chat',
      model: 'opus',
      archived: false,
      local: true,
      created_at: '2026-08-01T00:00:00Z',
      last_activity_at: '2026-08-01T00:00:00Z',
      last_read_at: '2026-08-02T00:00:00Z',
    },
    {
      chat_id: 'chat-unread',
      project_id: 'project-1',
      title: 'Unread chat',
      model: 'opus',
      archived: false,
      local: true,
      created_at: '2026-08-01T00:00:00Z',
      last_activity_at: '2026-08-03T00:00:00Z',
      last_read_at: '2026-08-01T00:00:00Z',
    },
  ] as unknown as typeof store.chats
  store.workspaces = [{ name: 'personal', color: 'emerald' }] as unknown as typeof store.workspaces
  store.projectStreaming = {}
  store.backgroundAgents = {}
  store.bootstrapped = true
  const taskStore = useTaskStore()
  taskStore.schedules = [] as unknown as typeof taskStore.schedules
  // ProjectView fetches these itself so it can tell "none" from "not loaded".
  // Stub them so the tests drive the store directly and stay deterministic.
  vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()
  return store
}

async function mountView(options: { attach?: boolean } = {}) {
  const { default: ProjectView } = await import('../ProjectView.vue')
  const wrapper = mount(ProjectView, {
    props: { projectId: 'project-1' },
    // Focus assertions need the tabs in the real document.
    ...(options.attach ? { attachTo: document.body } : {}),
    global: { stubs: { PaneHeader: { template: '<div><slot name="title" /><slot name="actions" /></div>' } } },
  })
  // Let onMounted's reloadAll settle so the automations load state resolves.
  await flush()
  return wrapper
}

async function flush() {
  await nextTick()
  await Promise.resolve()
  await new Promise(resolve => setTimeout(resolve, 0))
  await nextTick()
}

const LINKED = { path: '/r/app', branch: 'main', dirty: true, missing: false }

describe('ProjectView repository section', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.restoreAllMocks()
    askConfirm.mockReset()
  })

  it('offers Link when no repository is linked', async () => {
    seed()
    const wrapper = await mountView()
    const section = wrapper.find('.project-repo')
    expect(section.exists()).toBe(true)
    expect(section.find('h3').text()).toBe('Repository')
    expect(section.find('.text-action').text()).toBe('Link')
    expect(section.text()).toContain('No repository linked')
  })

  it('shows path, branch and uncommitted changes', async () => {
    seed(LINKED)
    const wrapper = await mountView()
    const section = wrapper.find('.project-repo')
    expect(section.find('.repo-path').text()).toBe('/r/app')
    expect(section.find('.repo-branch').text()).toContain('main')
    expect(section.find('.repo-dirty').text()).toContain('uncommitted changes')
    expect(section.text()).toContain('Open AGENTS.md')
    seed({ ...LINKED, branch: '', dirty: false })
    const clean = await mountView()
    expect(clean.find('.repo-branch').text()).toContain('detached')
    expect(clean.find('.repo-clean').text()).toBe('clean')
  })

  it('warns about a missing folder and hides Open AGENTS.md', async () => {
    seed({ ...LINKED, missing: true })
    const wrapper = await mountView()
    expect(wrapper.find('.repo-missing').text()).toContain('missing')
    expect(wrapper.find('.project-repo').text()).not.toContain('Open AGENTS.md')
  })

  it('unlinks after confirmation', async () => {
    const store = seed(LINKED)
    const update = vi.spyOn(store, 'updateProject').mockResolvedValue({} as never)
    const wrapper = await mountView()
    const unlink = () => wrapper.findAll('.project-repo .text-action').find(b => b.text() === 'Unlink')!

    askConfirm.mockResolvedValueOnce(false)
    await unlink().trigger('click')
    await flush()
    expect(update).not.toHaveBeenCalled()

    askConfirm.mockResolvedValueOnce(true)
    await unlink().trigger('click')
    await flush()
    expect(update).toHaveBeenCalledWith('project-1', { repo_path: '' })
  })

  it("opens the repo's AGENTS.md in the file viewer", async () => {
    seed(LINKED)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, status: 200 }))
    const open = vi.spyOn(useFileViewerStore(), 'open').mockResolvedValue(undefined as never)
    const wrapper = await mountView()
    await wrapper.findAll('.project-repo .text-action').find(b => b.text() === 'Open AGENTS.md')!.trigger('click')
    await flush()
    expect(open).toHaveBeenCalledWith('/r/app/AGENTS.md')
    vi.unstubAllGlobals()
  })

  it('says so when the repo has no instructions file', async () => {
    seed(LINKED)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 404 }))
    const wrapper = await mountView()
    await wrapper.findAll('.project-repo .text-action').find(b => b.text() === 'Open AGENTS.md')!.trigger('click')
    await flush()
    expect(wrapper.find('.repo-error').text()).toContain('no AGENTS.md or CLAUDE.md')
    vi.unstubAllGlobals()
  })
})
