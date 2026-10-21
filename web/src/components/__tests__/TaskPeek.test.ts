// @vitest-environment jsdom

/**
 * The read-only task peek over a chat: the task's facts, its description and
 * notes, the one action to the full page, and its not-found, loading and error
 * states. No field is editable here; editing stays on the board.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { mount, type VueWrapper } from '@vue/test-utils'
import { defineComponent, h, nextTick } from 'vue'
import TaskPeek from '../TaskPeek.vue'
import { useItemPeekStore } from '../../stores/itemPeek'
import type { TaskDetail } from '../../lib/types'

vi.mock('../../lib/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), del: vi.fn() },
}))
vi.mock('../../stores/projects', () => ({
  useProjectStore: () => ({
    activeWorkspace: 'work',
    generalProject: () => null,
    workspaceProjects: [],
  }),
}))

const RouterLinkStub = defineComponent({
  name: 'RouterLink',
  props: { to: { type: [String, Object], required: true } },
  setup(props, { slots }) {
    return () => h('a', { 'data-to': JSON.stringify(props.to), href: '#' }, slots.default?.())
  },
})

function detail(overrides: Partial<TaskDetail> = {}): TaskDetail {
  return {
    id: 'ship',
    title: 'Ship the board',
    status: 'in_progress',
    project_id: '',
    due: '',
    assignee: 'agent',
    chat_id: 'chat-1',
    attempt_id: 'att-1',
    linked_chats: [],
    created_at: '2026-03-01T09:00:00+00:00',
    updated_at: '2026-03-01T09:00:00+00:00',
    revision: 'a'.repeat(64),
    relative_path: 'Tasks/ship.md',
    attempt_state: 'running',
    attempt_outcome: '',
    attempt_summary: '',
    attempt_detail: '',
    live_attempt_id: 'att-1',
    completed_at: null,
    has_resolution: false,
    body: 'Write the **peek** view.',
    resolution: '',
    completions: [],
    delegation_log: '',
    updates: [
      { id: 'new', recorded_at: '2026-03-02T09:00:00+00:00', actor: 'agent', text: 'Checked the index', edited_at: null, attempt_id: 'att-1', chat_id: 'chat-1' },
      { id: 'old', recorded_at: '2026-03-01T09:00:00+00:00', actor: 'user', text: 'Started', edited_at: null, attempt_id: 'att-1', chat_id: 'chat-1' },
    ],
    ...overrides,
  }
}

let wrapper: VueWrapper | null = null

function mountPeek() {
  const host = defineComponent({
    setup() {
      return () => h(TaskPeek)
    },
  })
  wrapper = mount(host, {
    attachTo: document.body,
    global: { stubs: { RouterLink: RouterLinkStub } },
  })
  return wrapper
}

function dialog(): HTMLElement {
  const el = document.querySelector<HTMLElement>('[role="dialog"]')
  if (!el) throw new Error('no dialog rendered')
  return el
}

beforeEach(() => {
  setActivePinia(createPinia())
})

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  document.body.innerHTML = ''
})

describe('TaskPeek', () => {
  it('shows the task: its title, status and project, the description, and notes newest first', async () => {
    const peek = useItemPeekStore()
    peek.target = { kind: 'task', id: 'ship', workspace: 'work' }
    peek.task = detail({ due: '2999-01-01' })
    mountPeek()
    await nextTick()

    expect(dialog().textContent).toContain('Ship the board')
    expect(dialog().textContent).toContain('In progress')
    expect(dialog().textContent).toContain('General')
    expect(dialog().querySelector('.task-peek-description strong')?.textContent).toBe('peek')

    const notes = dialog().querySelectorAll('.task-note')
    expect(notes).toHaveLength(2)
    expect(notes[0]!.getAttribute('data-actor')).toBe('agent')
    expect(notes[0]!.textContent).toContain('Checked the index')
    expect(notes[1]!.getAttribute('data-actor')).toBe('user')
  })

  it('offers no field to edit', async () => {
    const peek = useItemPeekStore()
    peek.target = { kind: 'task', id: 'ship', workspace: 'work' }
    peek.task = detail()
    mountPeek()
    await nextTick()

    const body = dialog()
    expect(body.querySelector('textarea')).toBeNull()
    expect(body.querySelector('select')).toBeNull()
    expect(body.querySelector('input')).toBeNull()
    expect(body.querySelector('.task-note-edit')).toBeNull()
    expect(Array.from(body.querySelectorAll('button')).some((b) => /^edit/i.test(b.textContent?.trim() ?? ''))).toBe(false)
  })

  it('links to the full task page', async () => {
    const peek = useItemPeekStore()
    peek.target = { kind: 'task', id: 'ship', workspace: 'work' }
    peek.task = detail()
    mountPeek()
    await nextTick()

    const link = Array.from(dialog().querySelectorAll('a')).find((a) => a.textContent?.trim() === 'Open task page')
    expect(link).toBeTruthy()
    expect(JSON.parse(link!.getAttribute('data-to')!)).toEqual({ name: 'task-detail', params: { taskId: 'ship' } })
  })

  it('a task missing from the workspace says so and links to the board', async () => {
    const peek = useItemPeekStore()
    peek.target = { kind: 'task', id: 'gone', workspace: 'work' }
    peek.notFound = true
    mountPeek()
    await nextTick()

    expect(dialog().textContent).toContain("This task isn't on the work board.")
    const link = Array.from(dialog().querySelectorAll('a')).find((a) => a.textContent?.includes('board'))
    expect(JSON.parse(link!.getAttribute('data-to')!)).toEqual({ name: 'tasks' })
  })

  it('shows a loading status while the task is read', async () => {
    const peek = useItemPeekStore()
    peek.target = { kind: 'task', id: 'ship', workspace: 'work' }
    peek.loading = true
    mountPeek()
    await nextTick()

    const status = dialog().querySelector('[role="status"]')
    expect(status?.textContent).toContain('Loading task')
  })

  it('a read error offers Retry, which re-reads the task', async () => {
    const peek = useItemPeekStore()
    peek.target = { kind: 'task', id: 'ship', workspace: 'work' }
    peek.error = 'The task is being edited'
    const refresh = vi.spyOn(peek, 'refresh').mockResolvedValue(undefined)
    mountPeek()
    await nextTick()

    expect(dialog().querySelector('[role="alert"]')?.textContent).toContain('The task is being edited')
    const retry = Array.from(dialog().querySelectorAll('button')).find((b) => b.textContent?.trim() === 'Retry')
    retry!.click()
    expect(refresh).toHaveBeenCalled()
  })
})
