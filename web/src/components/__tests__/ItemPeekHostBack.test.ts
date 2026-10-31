// @vitest-environment jsdom

/**
 * Back over the task overlay, against a real history router: the overlay's
 * history entry, the host's own close behind it, and what a refusal leaves.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h, nextTick } from 'vue'
import { createRouter, createWebHistory, type Router } from 'vue-router'
import ItemPeekHost from '../ItemPeekHost.vue'
import { OVERLAY_STATE_KEY } from '../../composables/useOverlayBackEntry'
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

const Empty = defineComponent({ render: () => h('div') })
let router: Router
let wrapper: ReturnType<typeof mount> | null = null
let opener: HTMLAnchorElement

async function settle() {
  await nextTick()
  await nextTick()
  await new Promise<void>(resolve => setTimeout(resolve, 0))
}

function marker(): unknown {
  return window.history.state?.[OVERLAY_STATE_KEY] ?? null
}

function nextPopstate(): Promise<void> {
  return new Promise(resolve => window.addEventListener('popstate', () => resolve(), { once: true }))
}

async function back(): Promise<void> {
  const popped = nextPopstate()
  window.history.back()
  await popped
  await settle()
  await flushPromises()
}

async function openOverlay() {
  window.history.replaceState(null, '', '/')
  router = createRouter({
    history: createWebHistory(),
    routes: [{ path: '/', component: Empty }, { path: '/chat/:chatId', component: Empty }],
  })
  await router.push('/chat/abc')
  await router.isReady()
  answerWith(BOARD)
  opener = document.createElement('a')
  opener.href = '#'
  opener.textContent = 'Reply to Ivo'
  document.body.append(opener)
  opener.focus()
  const peek = useItemPeekStore()
  peek.openTask(TASK_ID)
  wrapper = mount(ItemPeekHost, { attachTo: document.body, global: { plugins: [router] } })
  await settle()
  await flushPromises()
  return peek
}

describe('ItemPeekHost and the Back button', () => {
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
    ]
    store.activeWorkspace = 'personal'
  })

  afterEach(async () => {
    wrapper?.unmount()
    wrapper = null
    opener?.remove()
    await flushPromises()
    vi.restoreAllMocks()
  })

  it('Back closes the task overlay, keeps the chat and writes the title', async () => {
    const peek = await openOverlay()
    expect(typeof marker()).toBe('string')
    apiPatch.mockResolvedValue({
      workspace: 'personal',
      task: { ...task({ title: 'Renamed' }), revision: NEXT_REVISION, body: '' },
    })
    await wrapper!.get('#task-detail-name').setValue('Renamed')
    await nextTick()

    await back()
    if (peek.isOpen || marker() !== null) await settle()

    expect(apiPatch.mock.calls[0]![1]).toMatchObject({ title: 'Renamed' })
    expect(peek.isOpen).toBe(false)
    expect(window.location.pathname).toBe('/chat/abc')
    expect(router.currentRoute.value.path).toBe('/chat/abc')
    expect(marker()).toBeNull()
  })

  it('Back over a title that cannot be saved keeps the overlay, the draft and its entry', async () => {
    const peek = await openOverlay()
    const id = marker()
    await wrapper!.get('#task-detail-name').setValue('   ')
    await nextTick()

    await back()
    if (marker() === null) {
      await nextPopstate()
      await settle()
      await flushPromises()
    }

    expect(peek.isOpen).toBe(true)
    expect(wrapper!.get<HTMLTextAreaElement>('#task-detail-name').element.value).toBe('   ')
    expect(marker()).toBe(id)
    expect(window.location.pathname).toBe('/chat/abc')
    expect(apiPatch).not.toHaveBeenCalled()
  })

  it('Back returns focus to the link that opened it', async () => {
    const peek = await openOverlay()
    expect(document.activeElement).not.toBe(opener)

    await back()
    await settle()

    expect(peek.isOpen).toBe(false)
    expect(document.activeElement).toBe(opener)
  })
})
