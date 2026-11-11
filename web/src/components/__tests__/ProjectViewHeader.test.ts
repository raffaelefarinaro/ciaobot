// @vitest-environment jsdom

/** The project page's header is an open chat's: close, a title rail over the workspace's projects, pull to close. */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'
import { resetRailState } from '../../composables/useTitleRail'

const askConfirm = vi.hoisted(() => vi.fn())
vi.mock('../../lib/confirm', () => ({ askConfirm }))

const push = vi.hoisted(() => vi.fn(async () => {}))
vi.mock('vue-router', () => ({
  useRouter: () => ({ push, replace: vi.fn() }),
}))

function seed(count = 3) {
  const store = useProjectStore()
  const projects = [
    { project_id: 'general', name: 'General', is_auto: true, order: 9 },
    { project_id: 'kitchen', name: 'Kitchen', is_auto: false, order: 0, vault_folder: 'kitchen' },
    { project_id: 'garden', name: 'Garden', is_auto: false, order: 1 },
  ].slice(0, count).map(p => ({ workspace: 'personal', context: 'old context', created_at: '2026-08-01T00:00:00Z', ...p }))
  store.projects = projects as unknown as typeof store.projects
  store.chats = []
  store.workspaces = [{ name: 'personal', color: 'emerald' }] as unknown as typeof store.workspaces
  store.activeWorkspace = 'personal'
  store.projectStreaming = {}
  store.backgroundAgents = {}
  store.bootstrapped = true
  const taskStore = useTaskStore()
  taskStore.schedules = [] as unknown as typeof taskStore.schedules
  vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()
  return store
}

async function mountView(projectId = 'kitchen') {
  const { default: ProjectView } = await import('../ProjectView.vue')
  const wrapper = mount(ProjectView, {
    attachTo: document.body,
    props: { projectId },
    global: { stubs: { PaneHeader: { template: '<div class="pane-header"><slot name="title" /><slot name="actions" /></div>' } } },
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

describe('ProjectView header', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.restoreAllMocks()
    askConfirm.mockReset()
    push.mockClear()
    // Reduced motion: a keyboard step switches at once instead of after the slide.
    vi.stubGlobal('matchMedia', (q: string) => ({ matches: q.includes('reduce'), addEventListener() {}, removeEventListener() {} }))
  })
  afterEach(() => {
    resetRailState('project')
    vi.unstubAllGlobals()
    document.body.innerHTML = ''
  })

  it('has the shared close and a title rail over the projects in sidebar order', async () => {
    seed()
    const wrapper = await mountView()
    const close = wrapper.get('.close-btn')
    expect(close.attributes('aria-label')).toBe('Close project')
    expect(close.find('svg').exists()).toBe(true)
    const rail = wrapper.get('.title-rail')
    expect(rail.attributes('aria-label')).toBe('Projects in this workspace, 2 of 3')
    expect(wrapper.findAll('.title-rail-item').map(i => i.find('.title-rail-title').text())).toEqual(['General', 'Kitchen', 'Garden'])
    expect(wrapper.get('.title-rail-item[aria-current="page"]').text()).toContain('Kitchen')
    await close.trigger('click')
    expect(wrapper.emitted('close')).toHaveLength(1)
  })

  it('keeps the plain title for a lone project, and a double-click renames', async () => {
    seed(1)
    const wrapper = await mountView('general')
    expect(wrapper.find('.title-rail').exists()).toBe(false)
    await wrapper.get('.project-title').trigger('dblclick')
    expect(wrapper.find('input.title-input').exists()).toBe(true)
  })

  it('renames from a double-click on the rail\'s title', async () => {
    seed()
    const wrapper = await mountView()
    await wrapper.get('.title-rail-item[aria-current="page"]').trigger('dblclick')
    expect((wrapper.get('input.title-input').element as HTMLInputElement).value).toBe('Kitchen')
    expect(wrapper.find('.title-rail').exists()).toBe(false)
  })

  it('steps to the neighbour with the previous / next chord', async () => {
    seed()
    const wrapper = await mountView()
    const vm = wrapper.vm as unknown as { selectAdjacent: (d: -1 | 1) => boolean }
    expect(vm.selectAdjacent(1)).toBe(true)
    await flush()
    expect(push).toHaveBeenCalledWith('/project/garden')
  })

  it('does not step while the name or the context is being edited, or with the actions menu open', async () => {
    seed()
    const wrapper = await mountView()
    const vm = wrapper.vm as unknown as { selectAdjacent: (d: -1 | 1) => boolean }

    await wrapper.findAll('button').find(b => b.text() === 'Edit')!.trigger('click')
    expect(vm.selectAdjacent(1)).toBe(false)
    await wrapper.findAll('button').find(b => b.text() === 'Cancel')!.trigger('click')

    await wrapper.get('[aria-label="Project actions"]').trigger('click')
    await flush()
    expect(vm.selectAdjacent(1)).toBe(false)
    expect(push).not.toHaveBeenCalled()
  })

  it('asks before a swipe drops an unsaved context draft, and stays when refused', async () => {
    seed()
    const wrapper = await mountView()
    await wrapper.findAll('button').find(b => b.text() === 'Edit')!.trigger('click')
    await wrapper.get('textarea').setValue('a draft to keep')
    askConfirm.mockResolvedValueOnce(false)
    const { default: TitleRail } = await import('../TitleRail.vue')
    // The rail stays up while the context is edited, so a swipe reaches the guard.
    wrapper.findComponent(TitleRail).vm.$emit('select', 'garden')
    await flush()
    expect(askConfirm).toHaveBeenCalledTimes(1)
    expect(push).not.toHaveBeenCalled()
    expect((wrapper.get('textarea').element as HTMLTextAreaElement).value).toBe('a draft to keep')

    askConfirm.mockResolvedValueOnce(true)
    wrapper.findComponent(TitleRail).vm.$emit('select', 'garden')
    await flush()
    expect(push).toHaveBeenCalledWith('/project/garden')
  })

  it('passes a pull on the title up for pull to close', async () => {
    seed()
    const wrapper = await mountView()
    const { default: TitleRail } = await import('../TitleRail.vue')
    wrapper.findComponent(TitleRail).vm.$emit('pull', 80)
    wrapper.findComponent(TitleRail).vm.$emit('pullEnd', 140, 0.2)
    expect(wrapper.emitted('pull')).toEqual([[80]])
    expect(wrapper.emitted('pull-end')).toEqual([[140, 0.2]])
  })

  it('folds New chat and Complete into the ⋯ menu on a phone', async () => {
    vi.stubGlobal('matchMedia', (q: string) => ({ matches: q.includes('max-width') || q.includes('reduce'), addEventListener() {}, removeEventListener() {} }))
    seed()
    const wrapper = await mountView()
    expect(wrapper.find('.project-new-chat').exists()).toBe(false)
    expect(wrapper.findAll('button').some(b => b.text() === 'Complete')).toBe(false)
    await wrapper.get('[aria-label="Project actions"]').trigger('click')
    await flush()
    expect(wrapper.findAll('[role="menuitem"]').map(i => i.text())).toEqual(['New chat', 'Complete', 'Delete project'])
  })
})
