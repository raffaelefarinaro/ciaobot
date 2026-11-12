// @vitest-environment jsdom

/**
 * On the board's own route an open task is a page, like an open chat or
 * automation: the shared close, a title rail over the board's tasks in board
 * order within its current filter, Escape and pull to close back to /tasks, and
 * a switch to another task that never drops the open one's draft.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, nextTick } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import TaskBoardView from '../TaskBoardView.vue'
import TitleRail from '../TitleRail.vue'
import { useProjectStore } from '../../stores/projects'
import { resetRailState } from '../../composables/useTitleRail'
import type { Task, TaskRow } from '../../lib/types'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())
const apiPatch = vi.hoisted(() => vi.fn())
const apiDel = vi.hoisted(() => vi.fn())
const askConfirm = vi.hoisted(() => vi.fn(async () => true))
const askConfirmReason = vi.hoisted(() => vi.fn(async () => ({ confirmed: true, reason: '' })))
const pendingConfirm = vi.hoisted(() => ({ value: null as unknown }))

vi.mock('../../lib/api', () => ({ api: { get: apiGet, post: apiPost, patch: apiPatch, del: apiDel } }))
vi.mock('../../lib/confirm', () => ({ askConfirm, askConfirmReason, pendingConfirm }))

const REVISION = 'a'.repeat(64)
const NEXT_REVISION = 'b'.repeat(64)

function task(overrides: Partial<Task> = {}): Task {
  return {
    id: 'ship', title: 'Ship the board', status: 'backlog', project_id: '', due: '', assignee: 'user',
    chat_id: '', attempt_id: '', linked_chats: [], created_at: '2026-03-01T09:00:00+00:00',
    updated_at: '2026-03-01T09:00:00+00:00', revision: REVISION, relative_path: 'Tasks/ship.md',
    attempt_state: '', attempt_outcome: '', attempt_summary: '', attempt_detail: '', live_attempt_id: '',
    completed_at: overrides.status === 'done' ? '2026-03-01T08:00:00Z' : null, has_resolution: false,
    ...overrides,
  }
}

const BOARD: TaskRow[] = [
  task(),
  task({ id: 'plan', title: 'Plan the launch', status: 'backlog' }),
  task({ id: 'doing', title: 'Wire the store', status: 'in_progress', attempt_state: 'running' }),
  task({ id: 'held', title: 'Wait on a key', status: 'in_review' }),
]

async function mountAt(path: string) {
  const Stub = defineComponent({ render: () => null })
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/tasks', name: 'tasks', component: Stub },
      { path: '/tasks/:taskId', name: 'task-detail', component: Stub },
    ],
  })
  await router.push(path)
  apiGet.mockImplementation((url: string) => {
    if (url.includes('/api/tasks?')) return Promise.resolve({ workspace: 'personal', tasks: BOARD })
    const id = /\/api\/tasks\/([^?]+)/.exec(url)?.[1] ?? 'ship'
    const row = (BOARD as Task[]).find(t => t.id === id) ?? task()
    return Promise.resolve({ workspace: 'personal', task: { ...row, revision: NEXT_REVISION, body: '' } })
  })
  const wrapper = mount(TaskBoardView, { attachTo: document.body, global: { plugins: [router] } })
  await settle()
  return { wrapper, router }
}

async function settle() {
  await flushPromises()
  await nextTick()
  await flushPromises()
}

type Exposed = { selectAdjacent: (d: -1 | 1) => boolean; closeDetailPage: () => Promise<void>; openCreate: () => void }

describe('the open task as a page', () => {
  beforeEach(() => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-03-01T12:00:00Z'))
    localStorage.setItem('ciao.tasks.doneShown', '1')
    setActivePinia(createPinia())
    for (const fn of [apiGet, apiPost, apiPatch, apiDel, askConfirm]) fn.mockReset()
    askConfirm.mockResolvedValue(true)
    pendingConfirm.value = null
    vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
    // Reduced motion: a keyboard step switches at once instead of after the slide.
    vi.stubGlobal('matchMedia', (q: string) => ({ matches: q.includes('reduce'), addEventListener() {}, removeEventListener() {} }))
    const store = useProjectStore()
    store.workspaces = [{ name: 'personal', vault_root: '/tmp/vault', default_provider: 'claude', gws_profile: '' }]
    store.activeWorkspace = 'personal'
    store.projects = []
  })

  afterEach(() => {
    resetRailState('task')
    vi.useRealTimers()
    document.body.innerHTML = ''
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('has the shared header: close, and a rail over the tasks in board order', async () => {
    const { wrapper } = await mountAt('/tasks/plan')
    const page = wrapper.get('.task-detail-page')
    expect(page.find('.close-btn svg').exists()).toBe(true)
    const rail = page.get('.title-rail')
    expect(rail.attributes('aria-label')).toBe('Tasks on this board, 2 of 4')
    expect(page.findAll('.title-rail-item').map(i => i.attributes('aria-label'))).toEqual([
      'Ship the board, To do',
      'Plan the launch, To do',
      'Wire the store, In progress, working',
      'Wait on a key, In review',
    ])
    expect(page.findAll('.title-rail-seg').map(s => s.classes().includes('title-rail-seg--working'))).toEqual([false, false, true, false])
    wrapper.unmount()
  })

  it('follows the board\'s filter: a status filter narrows the rail, and a lone task keeps the plain title', async () => {
    const { wrapper } = await mountAt('/tasks')
    const chips = wrapper.findAll('[aria-label="Filter by status"] button')
    await chips.find(c => c.text().startsWith('In progress'))!.trigger('click')
    await wrapper.findAll('.task-card').find(c => c.text().includes('Wire the store'))!.get('.task-open').trigger('click')
    await settle()
    expect(wrapper.find('.task-detail-page .title-rail').exists()).toBe(false)
    expect(wrapper.get('.task-detail-page .pane-title').text()).toBe('Wire the store')
    wrapper.unmount()
  })

  it('steps to the next task with the chord, writing the open task first', async () => {
    const { wrapper, router } = await mountAt('/tasks/ship')
    apiPatch.mockResolvedValue({ workspace: 'personal', task: { ...task({ title: 'Ship it' }), revision: NEXT_REVISION, body: '' } })
    await wrapper.get('#task-detail-name').setValue('Ship it')
    const pageBefore = wrapper.get('.task-detail-page').element
    expect((wrapper.vm as unknown as Exposed).selectAdjacent(1)).toBe(true)
    await settle()
    expect(apiPatch).toHaveBeenCalledTimes(1)
    expect(router.currentRoute.value.path).toBe('/tasks/plan')
    expect((wrapper.get('#task-detail-name').element as HTMLTextAreaElement).value).toBe('Plan the launch')
    // The step commits the open task and opens the next in place: the page is
    // the same node, the board never came back, and the body arrives from the
    // side the rail moved to.
    expect(wrapper.get('.task-detail-page').element).toBe(pageBefore)
    expect(wrapper.get('.task-view > .page-grid').attributes('style')).toContain('display: none')
    expect(wrapper.get('.task-detail-pane').classes()).toContain('rail-arrive-next')
    // The rail keeps its travel order and lands on the next item.
    expect(wrapper.get('.title-rail').attributes('aria-label')).toBe('Tasks on this board, 2 of 4')
    wrapper.unmount()
  })

  it('leaves an Escape aimed outside the page to its owner', async () => {
    const { wrapper, router } = await mountAt('/tasks/ship')
    // A popover field outside the page (the sidebar's rename, the bell's sheet)
    // that answers its own Escape.
    const outside = document.createElement('input')
    document.body.appendChild(outside)
    const owner = vi.fn()
    outside.addEventListener('keydown', (e) => { if (e.key === 'Escape') owner() })
    outside.focus()
    outside.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))
    await settle()
    expect(owner).toHaveBeenCalledTimes(1)
    expect(wrapper.find('.task-detail-page').exists()).toBe(true)
    expect(router.currentRoute.value.path).toBe('/tasks/ship')

    // Aimed at the page, it closes it.
    wrapper.get('#task-detail-name').element.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))
    await settle()
    expect(wrapper.find('.task-detail-page').exists()).toBe(false)
    wrapper.unmount()
  })

  it('folds Delete and the save line into a ⋯ menu that a phone shows instead', async () => {
    const { wrapper } = await mountAt('/tasks/ship')
    const actions = wrapper.get('.task-detail-page .pane-header')
    expect(actions.find('.task-delete.fold-wide').exists()).toBe(true)
    // The shared ⋯ menu, its trigger shown only where the header folds.
    const trigger = actions.get('.item-menu-trigger')
    expect(trigger.attributes('aria-label')).toBe('More task actions')
    expect(trigger.classes()).toContain('item-menu-trigger--phone')
    expect(actions.get('.task-saved').attributes('role')).toBe('status')
    wrapper.unmount()
  })

  it('never drops a draft it cannot write: a blank title keeps the task, its sentence and its address', async () => {
    const { wrapper, router } = await mountAt('/tasks/ship')
    await wrapper.get('#task-detail-name').setValue('')
    wrapper.findComponent(TitleRail).vm.$emit('select', 'plan')
    await settle()
    expect(router.currentRoute.value.path).toBe('/tasks/ship')
    expect((wrapper.get('#task-detail-name').element as HTMLTextAreaElement).value).toBe('')
    expect(wrapper.get('.task-detail-page').text()).toContain('Add a title before closing')
    wrapper.unmount()
  })

  it('keeps the draft when another task is opened from its address (the sidebar) and the write is refused', async () => {
    const { wrapper, router } = await mountAt('/tasks/ship')
    apiPatch.mockRejectedValue(Object.assign(new Error('HTTP 409'), {
      payload: { error: { code: 'task_revision_conflict', message: 'that task changed on disk', retryable: true } },
    }))
    await wrapper.get('#task-detail-name').setValue('Renamed')
    await router.push('/tasks/plan')
    await settle()
    expect((wrapper.get('#task-detail-name').element as HTMLTextAreaElement).value).toBe('Renamed')
    expect(wrapper.get('.task-detail-page').text()).toContain('that task changed on disk')
    expect(router.currentRoute.value.path).toBe('/tasks/ship')
    wrapper.unmount()
  })

  it('does not step under a confirm or a sheet', async () => {
    const { wrapper, router } = await mountAt('/tasks/ship')
    const vm = wrapper.vm as unknown as Exposed
    pendingConfirm.value = { message: 'Delete?' }
    expect(vm.selectAdjacent(1)).toBe(false)
    pendingConfirm.value = null
    vm.openCreate()
    await settle()
    expect(vm.selectAdjacent(1)).toBe(false)
    expect(router.currentRoute.value.path).toBe('/tasks/ship')
    wrapper.unmount()
  })

  it('closes back to the board on Escape and through the host\'s pull-to-close', async () => {
    const { wrapper, router } = await mountAt('/tasks/ship')
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))
    await settle()
    expect(wrapper.find('.task-detail-page').exists()).toBe(false)
    expect(router.currentRoute.value.path).toBe('/tasks')

    await router.push('/tasks/plan')
    await settle()
    wrapper.findComponent(TitleRail).vm.$emit('pull', 90)
    wrapper.findComponent(TitleRail).vm.$emit('pullEnd', 150, 0.3)
    expect(wrapper.emitted('pull')).toEqual([[90]])
    expect(wrapper.emitted('pull-end')).toEqual([[150, 0.3]])
    await (wrapper.vm as unknown as Exposed).closeDetailPage()
    await settle()
    expect(router.currentRoute.value.path).toBe('/tasks')
    wrapper.unmount()
  })
})
