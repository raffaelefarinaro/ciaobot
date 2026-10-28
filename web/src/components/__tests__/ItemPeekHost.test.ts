// @vitest-environment jsdom

/**
 * The one mount point for item overlays.
 *
 * It owns the real task surface in overlay mode: the editor the user gets is
 * the board's editor, not a read-only peek, and the host is what keeps it over
 * the screen the link was on. What only this seam can decide: the surface stays
 * mounted across its own internal close (the delegation preview), it unmounts
 * on the final `close`, navigation is refused rather than dropped while a draft
 * cannot be saved, a workspace switch is asked first, and focus returns to the
 * link that opened it.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick, reactive } from 'vue'
import { routeLocationKey, routerKey } from 'vue-router'
import ItemPeekHost from '../ItemPeekHost.vue'
import TaskWorkspaceSurface from '../TaskWorkspaceSurface.vue'
import { useItemPeekStore } from '../../stores/itemPeek'
import { useProjectStore } from '../../stores/projects'
import type { Task, TaskRow } from '../../lib/types'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())
const apiPatch = vi.hoisted(() => vi.fn())
const apiDel = vi.hoisted(() => vi.fn())
const askConfirm = vi.hoisted(() => vi.fn(async () => true))
const askConfirmReason = vi.hoisted(() => vi.fn(async () => ({ confirmed: true, reason: '' })))
const pendingConfirm = vi.hoisted(() => ({ value: null as unknown }))

vi.mock('../../lib/api', () => ({
  api: { get: apiGet, post: apiPost, patch: apiPatch, del: apiDel },
}))

vi.mock('../../lib/confirm', () => ({
  askConfirm,
  askConfirmReason,
  pendingConfirm,
}))

const REVISION = 'a'.repeat(64)
const NEXT_REVISION = 'b'.repeat(64)
const TASK_ID = 'ship'

function task(overrides: Partial<Task> = {}): Task {
  return {
    id: TASK_ID,
    title: 'Ship the board',
    status: 'in_progress',
    project_id: '',
    due: '',
    assignee: 'agent',
    chat_id: '',
    attempt_id: '',
    linked_chats: [],
    created_at: '2026-03-01T09:00:00+00:00',
    updated_at: '2026-03-01T09:00:00+00:00',
    revision: REVISION,
    relative_path: 'Tasks/ship.md',
    attempt_state: '',
    attempt_outcome: '',
    attempt_summary: '',
    attempt_detail: '',
    live_attempt_id: '',
    completed_at: null,
    has_resolution: false,
    ...overrides,
  }
}

const BOARD: TaskRow[] = [task({ id: 'doing', title: 'Wire the store' }), task()]

function detailAnswer(row: Task, body = 'Prose the list never carried.') {
  return { workspace: 'personal', task: { ...row, revision: NEXT_REVISION, body } }
}

/** List reads answer with rows; one task's read answers with its own record. */
function answerWith(rows: TaskRow[], body = 'Prose the list never carried.') {
  apiGet.mockImplementation((url: string) => {
    if (url.startsWith('/api/tasks?')) {
      return Promise.resolve({ workspace: 'personal', tasks: rows })
    }
    const id = url.slice('/api/tasks/'.length).split('?')[0]!
    const row = rows.find((entry) => entry.id === id) ?? task({ id })
    return Promise.resolve(detailAnswer(row as Task, body))
  })
}

/**
 * A router with the two things the host uses: `beforeEach`, and a `push` that
 * asks every registered guard so a cancel is observable.
 */
function fakeRouter() {
  const guards: Array<() => unknown> = []
  const push = vi.fn(async (_to: string) => {
    for (const guard of [...guards]) {
      if (await guard() === false) return false
    }
    return true
  })
  const beforeEach = vi.fn((guard: () => unknown) => {
    guards.push(guard)
    return () => {
      const index = guards.indexOf(guard)
      if (index !== -1) guards.splice(index, 1)
    }
  })
  return { beforeEach, push, guards }
}

function render(router: ReturnType<typeof fakeRouter>) {
  return mount(ItemPeekHost, {
    attachTo: document.body,
    global: {
      provide: {
        [routeLocationKey as symbol]: reactive({ fullPath: '/chat/abc', params: {} }),
        [routerKey as symbol]: router,
      },
    },
  })
}

const surface = (wrapper: ReturnType<typeof mount>) => wrapper.findComponent(TaskWorkspaceSurface)
const editor = (wrapper: ReturnType<typeof mount>) => wrapper.find('.task-detail-sheet')

beforeEach(() => {
  setActivePinia(createPinia())
  apiGet.mockReset()
  apiPost.mockReset()
  apiPatch.mockReset()
  apiDel.mockReset()
  askConfirm.mockReset()
  askConfirmReason.mockReset()
  askConfirm.mockResolvedValue(true)
  askConfirmReason.mockResolvedValue({ confirmed: true, reason: '' })
  pendingConfirm.value = null

  const store = useProjectStore()
  store.workspaces = [
    { name: 'personal', vault_root: '/tmp/vault', default_provider: 'claude', gws_profile: '' },
    { name: 'work', vault_root: '/tmp/work', default_provider: 'claude', gws_profile: '' },
  ]
  store.activeWorkspace = 'personal'
  store.projects = [
    { project_id: 'general', name: 'General', workspace: 'personal', context: '', created_at: '2026-03-01', order: 1, vault_folder: '', is_auto: true },
  ]
})

afterEach(() => {
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

describe('ItemPeekHost', () => {
  it('mounts the real editor over the current view, for the target the store holds', async () => {
    answerWith(BOARD)
    const peek = useItemPeekStore()
    peek.openTask(TASK_ID)
    const wrapper = render(fakeRouter())
    await flushPromises()
    await nextTick()

    // The board editor, over the chat, with no board chrome and no URL change.
    expect(surface(wrapper).props()).toMatchObject({ mode: 'overlay', taskId: TASK_ID })
    expect(wrapper.find('.task-lane').exists()).toBe(false)
    expect(editor(wrapper).exists()).toBe(true)
    expect(editor(wrapper).get<HTMLTextAreaElement>('#task-detail-name').element.value).toBe('Ship the board')
    expect(wrapper.text()).toContain('Prose the list never carried.')
    wrapper.unmount()
  })

  it('unmounts the surface only when it finishes its close', async () => {
    answerWith(BOARD)
    const peek = useItemPeekStore()
    peek.openTask(TASK_ID)
    const wrapper = render(fakeRouter())
    await flushPromises()
    await nextTick()

    const closed = await (surface(wrapper).vm as unknown as { requestClose: () => Promise<boolean> }).requestClose()
    expect(closed).toBe(true)
    await flushPromises()
    await nextTick()

    expect(peek.isOpen).toBe(false)
    expect(surface(wrapper).exists()).toBe(false)
    expect(editor(wrapper).exists()).toBe(false)
    wrapper.unmount()
  })

  it('keeps the surface mounted across the internal close before the delegation preview', async () => {
    answerWith(BOARD)
    const peek = useItemPeekStore()
    peek.openTask(TASK_ID)
    const wrapper = render(fakeRouter())
    await flushPromises()
    await nextTick()

    await editor(wrapper).get('.task-delegate-go').trigger('click')
    await flushPromises()
    await nextTick()

    // The editor closed so the preview is not drawn under a second modal. That
    // is not a closure the host may unmount on: the preview is the surface.
    expect(editor(wrapper).exists()).toBe(false)
    expect(wrapper.find('#task-delegate-title').exists()).toBe(true)
    expect(peek.isOpen).toBe(true)
    expect(surface(wrapper).exists()).toBe(true)
    wrapper.unmount()
  })

  it('refuses a navigation while the draft cannot be saved', async () => {
    answerWith(BOARD)
    const peek = useItemPeekStore()
    peek.openTask(TASK_ID)
    const router = fakeRouter()
    const wrapper = render(router)
    await flushPromises()
    await nextTick()

    await wrapper.get('#task-detail-name').setValue('   ')
    await nextTick()

    // Back/Forward and a router push both come through this guard: the route
    // does not move, and the form keeps what was typed.
    expect(await router.push('/chat/b')).toBe(false)
    expect(peek.isOpen).toBe(true)
    expect(editor(wrapper).exists()).toBe(true)
    expect(editor(wrapper).get<HTMLTextAreaElement>('#task-detail-name').element.value).toBe('   ')
    expect(apiPatch).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('lets navigation proceed once the close wrote what the editor held', async () => {
    answerWith(BOARD)
    const peek = useItemPeekStore()
    peek.openTask(TASK_ID)
    const router = fakeRouter()
    const wrapper = render(router)
    await flushPromises()
    await nextTick()

    apiPatch.mockResolvedValue({
      workspace: 'personal',
      task: { ...task({ title: 'Renamed' }), revision: NEXT_REVISION, body: '' },
    })
    await wrapper.get('#task-detail-name').setValue('Renamed')
    await nextTick()

    expect(await router.push('/chat/b')).toBe(true)
    expect(apiPatch.mock.calls[0]![1]).toMatchObject({ title: 'Renamed' })
    await flushPromises()
    await nextTick()
    expect(peek.isOpen).toBe(false)
    expect(surface(wrapper).exists()).toBe(false)
    wrapper.unmount()
  })

  it('refuses a workspace switch while the draft cannot be saved', async () => {
    answerWith(BOARD)
    const peek = useItemPeekStore()
    peek.openTask(TASK_ID)
    const wrapper = render(fakeRouter())
    await flushPromises()
    await nextTick()

    await wrapper.get('#task-detail-name').setValue('   ')
    await nextTick()

    const store = useProjectStore()
    expect(await store.switchWorkspace('work', { transition: false })).toBe(false)
    expect(store.activeWorkspace).toBe('personal')
    expect(peek.isOpen).toBe(true)
    expect(editor(wrapper).exists()).toBe(true)
    wrapper.unmount()
  })

  it('closes the overlay before an allowed workspace switch', async () => {
    answerWith(BOARD)
    const peek = useItemPeekStore()
    peek.openTask(TASK_ID)
    const wrapper = render(fakeRouter())
    await flushPromises()
    await nextTick()

    const store = useProjectStore()
    expect(await store.switchWorkspace('work', { transition: false })).toBe(true)
    expect(store.activeWorkspace).toBe('work')
    expect(peek.isOpen).toBe(false)
    await flushPromises()
    await nextTick()
    expect(editor(wrapper).exists()).toBe(false)
    wrapper.unmount()
  })

  it('unregisters its route and workspace guards when the host goes away', async () => {
    answerWith(BOARD)
    const peek = useItemPeekStore()
    peek.openTask(TASK_ID)
    const router = fakeRouter()
    const wrapper = render(router)
    await flushPromises()
    await nextTick()
    expect(router.guards).toHaveLength(1)

    wrapper.unmount()
    expect(router.guards).toHaveLength(0)
    // A host that is gone must not keep refusing a workspace switch.
    const store = useProjectStore()
    expect(await store.switchWorkspace('work', { transition: false })).toBe(true)
    expect(store.activeWorkspace).toBe('work')
  })

  it('returns focus to the element that opened the overlay', async () => {
    answerWith(BOARD)
    const peek = useItemPeekStore()
    peek.openTask(TASK_ID)
    document.body.innerHTML = '<a href="#" class="opener">Reply to Ivo</a>'
    const opener = document.querySelector<HTMLElement>('.opener')!
    opener.focus()

    const wrapper = render(fakeRouter())
    await flushPromises()
    await nextTick()
    expect(editor(wrapper).get('#task-detail-name').attributes('id')).toBe('task-detail-name')
    expect(document.activeElement).not.toBe(opener)

    await (surface(wrapper).vm as unknown as { requestClose: () => Promise<boolean> }).requestClose()
    await flushPromises()
    await nextTick()
    await nextTick()

    expect(peek.isOpen).toBe(false)
    expect(document.activeElement).toBe(opener)
    wrapper.unmount()
  })
})
