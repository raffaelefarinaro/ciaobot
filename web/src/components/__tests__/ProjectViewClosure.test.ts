// @vitest-environment jsdom

/**
 * The project closure sheet: one action, and a note that is optional in the
 * field itself — the same contract the task board's completion sheet carries
 * (#1608) and the chat rail's task origin carries (#1837).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())
vi.mock('../../lib/api', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../lib/api')>()),
  api: { get: apiGet, post: apiPost, patch: vi.fn(), put: vi.fn(), delete: vi.fn() },
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
    // A project that lives in the vault can be closed from its page.
    vault_folder: 'memory-vault/Projects/Upwordo',
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
  await flushPromises()
  await nextTick()
}

async function openClosureSheet() {
  seed()
  vi.stubGlobal('fetch', vi.fn(async () => new Response('[]')))
  const { default: ProjectView } = await import('../ProjectView.vue')
  const wrapper = mount(ProjectView, {
    props: { projectId: 'project-1' },
    global: { stubs: { PaneHeader: { template: '<div><slot name="title" /><slot name="actions" /></div>' } } },
  })
  await flush()

  const opener = wrapper.findAll('button').find(b => b.text().trim() === 'Complete')
  if (!opener) throw new Error('Missing the Complete action')
  await opener.trigger('click')
  await flush()
  return wrapper
}

describe('ProjectView closure sheet', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.restoreAllMocks()
    apiGet.mockReset()
    apiPost.mockReset()
    apiGet.mockImplementation(async (url: string) => {
      if (url.includes('/api/tasks?')) return { tasks: [] }
      throw new Error(`unstubbed ${url}`)
    })
    apiPost.mockResolvedValue({})
  })

  it('offers one closure action and Cancel, and no no-note button', async () => {
    const wrapper = await openClosureSheet()

    const actions = wrapper.get('.modal-sheet .form-actions')
    expect(actions.findAll('button').map(b => b.text().trim())).toEqual(['Complete project', 'Cancel'])
    expect(wrapper.get('.modal-sheet').text()).not.toContain('Close without note')
    wrapper.unmount()
  })

  it('closes the project with an empty note when the note is left blank', async () => {
    const wrapper = await openClosureSheet()

    // The note is optional in the field, so a blank draft leaves the action
    // available: the sheet never asks for a note before it lets the user close.
    const submit = wrapper.get('.modal-sheet .form-actions .btn-primary')
    expect((submit.element as HTMLButtonElement).disabled).toBe(false)

    await wrapper.get('.modal-sheet form').trigger('submit')
    await flush()

    expect(apiPost).toHaveBeenCalledTimes(1)
    expect(apiPost).toHaveBeenCalledWith('/api/projects/project-1/complete', {
      outcome: 'completed',
      note: '',
    })
    wrapper.unmount()
  })

  it('closes the project with an empty note when the note is only whitespace', async () => {
    const wrapper = await openClosureSheet()

    // A draft of spaces is no note at all: it closes the project the same way
    // a blank field does, rather than travelling to the engine as padding.
    await wrapper.get('#project-closure-note').setValue('   ')
    await wrapper.get('.modal-sheet form').trigger('submit')
    await flush()

    expect(apiPost).toHaveBeenCalledTimes(1)
    expect(apiPost).toHaveBeenCalledWith('/api/projects/project-1/complete', {
      outcome: 'completed',
      note: '',
    })
    wrapper.unmount()
  })

  it('sends the typed note trimmed', async () => {
    const wrapper = await openClosureSheet()

    await wrapper.get('#project-closure-note').setValue('  Shipped the release  ')
    await wrapper.get('.modal-sheet form').trigger('submit')
    await flush()

    expect(apiPost).toHaveBeenCalledTimes(1)
    expect(apiPost).toHaveBeenCalledWith('/api/projects/project-1/complete', {
      outcome: 'completed',
      note: 'Shipped the release',
    })
    wrapper.unmount()
  })
})
