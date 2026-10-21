// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'

const askConfirm = vi.hoisted(() => vi.fn())
vi.mock('../../lib/confirm', () => ({ askConfirm }))

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}))

function seed() {
  const store = useProjectStore()
  store.projects = [{
    project_id: 'project-1',
    name: 'Kitchen',
    workspace: 'personal',
    context: '',
    created_at: '2026-08-01T00:00:00Z',
    order: 0,
    vault_folder: 'kitchen',
    is_auto: false,
  }] as unknown as typeof store.projects
  store.chats = [{
    chat_id: 'chat-1',
    project_id: 'project-1',
    title: 'Kitchen chat',
    model: 'opus',
    archived: false,
    local: true,
    created_at: '2026-08-01T00:00:00Z',
    last_activity_at: '2026-08-01T00:00:00Z',
    last_read_at: '2026-08-01T00:00:00Z',
  }] as unknown as typeof store.chats
  store.workspaces = [{ name: 'personal', color: 'emerald' }] as unknown as typeof store.workspaces
  store.projectStreaming = {}
  store.backgroundAgents = {}
  store.bootstrapped = true
  const taskStore = useTaskStore()
  taskStore.schedules = [] as unknown as typeof taskStore.schedules
  vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()
  return store
}

async function mountView() {
  const { default: ProjectView } = await import('../ProjectView.vue')
  const wrapper = mount(ProjectView, {
    props: { projectId: 'project-1' },
    global: { stubs: { PaneHeader: { template: '<div><slot name="title" /><slot name="actions" /></div>' } } },
  })
  await flush()
  return wrapper
}

async function flush() {
  await nextTick()
  await Promise.resolve()
  await new Promise(resolve => setTimeout(resolve, 0))
  await nextTick()
}

describe('ProjectView delete for a vault-backed project', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.restoreAllMocks()
    askConfirm.mockReset()
  })

  it('offers Complete and Delete on a vault-backed project', async () => {
    // The actions menu used to be hidden for a vault-backed project, so the
    // page offered only Complete and no way to delete the project at all.
    seed()
    const wrapper = await mountView()

    expect(wrapper.findAll('button').some(b => b.text() === 'Complete')).toBe(true)

    const trigger = wrapper.get('[aria-label="Project actions"]')
    await trigger.trigger('click')
    await flush()
    const items = wrapper.findAll('[role="menuitem"]').map(item => item.text())
    expect(items).toContain('Delete project')
  })

  it('confirms with the folder name and deletes the project', async () => {
    const store = seed()
    askConfirm.mockResolvedValue(true)
    const deleteProject = vi.spyOn(store, 'deleteProject').mockResolvedValue({
      ok: true,
      vault_entry_removed: true,
      removed_notes: 2,
      removed_files: 3,
      edited_backlinks: [],
    })
    const wrapper = await mountView()

    await wrapper.get('[aria-label="Project actions"]').trigger('click')
    await flush()
    await wrapper.findAll('[role="menuitem"]').find(i => i.text() === 'Delete project')!.trigger('click')
    await flush()

    expect(askConfirm).toHaveBeenCalled()
    const message = askConfirm.mock.calls.at(-1)![0] as string
    expect(message).toContain('projects/active/kitchen/')
    expect(deleteProject).toHaveBeenCalledWith('project-1')
  })
})
