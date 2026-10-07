// @vitest-environment jsdom

/**
 * The delegated chat's origin note: the task it works on, the agent's state in
 * words, and the Approve Done that completes a reviewed result from the chat.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h } from 'vue'
import TaskOriginNote from '../TaskOriginNote.vue'
import { useTaskSignalsStore } from '../../stores/taskSignals'
import type { ChatInfo, Task } from '../../lib/types'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())
vi.mock('../../lib/api', () => ({ api: { get: apiGet, post: apiPost } }))
const askConfirm = vi.hoisted(() => vi.fn())
vi.mock('../../lib/confirm', () => ({ askConfirm }))

const RouterLinkStub = defineComponent({
  name: 'RouterLink',
  props: { to: { type: [String, Object], required: true } },
  setup(props, { slots }) {
    return () => h('a', { 'data-to': JSON.stringify(props.to), href: '#' }, slots.default?.())
  },
})

const REVISION = 'a'.repeat(64)

function task(overrides: Partial<Task> = {}): Task {
  return {
    id: 'ship',
    title: 'Ship the board',
    status: 'in_progress',
    project_id: '',
    due: '',
    assignee: 'agent',
    chat_id: 'chat-1',
    attempt_id: 'att-1',
    created_at: '',
    updated_at: '',
    revision: REVISION,
    relative_path: 'Tasks/ship.md',
    attempt_state: 'running',
    attempt_outcome: '',
    attempt_summary: '',
    attempt_detail: '',
    live_attempt_id: 'att-1',
    changed_since_delegated: false,
    ...overrides,
  }
}

const CHAT: Pick<ChatInfo, 'chat_id' | 'helper'> = {
  chat_id: 'chat-1',
  helper: { kind: 'task_delegation', task_id: 'ship', task_revision: REVISION, attempt_id: 'att-1' },
}

function mountNote(tasks: Task[], chat = CHAT, variant: 'rail' | 'note' = 'rail') {
  const signals = useTaskSignalsStore()
  signals.tasks = tasks
  signals.loadedWorkspace = 'personal'
  return mount(TaskOriginNote, {
    props: { chat, variant },
    global: { stubs: { RouterLink: RouterLinkStub } },
  })
}

describe('TaskOriginNote', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    apiGet.mockReset()
    apiPost.mockReset()
    askConfirm.mockReset()
    askConfirm.mockResolvedValue(true)
    apiGet.mockResolvedValue({ workspace: 'personal', tasks: [] })
  })
  afterEach(() => { vi.restoreAllMocks() })

  it('names the task, links to its editor, and says the agent is running', () => {
    const wrapper = mountNote([task()])
    expect(wrapper.text()).toContain('This chat works on the task Ship the board.')
    const link = wrapper.get('a')
    expect(JSON.parse(link.attributes('data-to')!)).toEqual({ name: 'task-detail', params: { taskId: 'ship' } })
    expect(wrapper.get('.task-origin-status').text()).toBe('Running')
    // The user can always override: detaching stops the turn.
    expect(wrapper.get('button').text()).toBe('Mark done')
  })

  it('says the agent needs input, and still lets the user mark it done', () => {
    const wrapper = mountNote([task({ status: 'in_progress', attempt_state: 'needs_you', attempt_outcome: 'needs_input' })])
    expect(wrapper.get('.task-origin-status').text()).toBe('Needs input')
    expect(wrapper.get('button').text()).toBe('Mark done')
  })

  it('Mark done overrides the attempt: asks, detaches, completes, then hands the archive back', async () => {
    const wrapper = mountNote([task({ attempt_state: 'needs_you', attempt_detail: 'the turn ended without a report from the agent' })])
    const NEXT = 'b'.repeat(64)
    apiPost.mockImplementation((url: string) => Promise.resolve(
      url.endsWith('/detach') ? { task: { revision: NEXT } } : { task: { status: 'done' } },
    ))
    await wrapper.get('button').trigger('click')
    await flushPromises()

    expect(askConfirm).toHaveBeenCalledTimes(1)
    expect(apiPost.mock.calls.map((c) => c[0])).toEqual([
      '/api/tasks/ship/attempt/att-1/detach',
      '/api/tasks/ship/complete',
    ])
    expect(apiPost.mock.calls[1]![1]).toEqual({ workspace: 'personal', expected_revision: NEXT })
    expect(wrapper.emitted('marked-done')).toHaveLength(1)
  })

  it('Mark done does nothing when the user cancels', async () => {
    askConfirm.mockResolvedValue(false)
    const wrapper = mountNote([task({ attempt_state: 'interrupted', live_attempt_id: '' })])
    await wrapper.get('button').trigger('click')
    await flushPromises()
    expect(apiPost).not.toHaveBeenCalled()
    expect(wrapper.emitted('marked-done')).toBeUndefined()
  })

  it('offers Approve Done for a result waiting on review, at the revision read', async () => {
    const reviewed = task({ status: 'in_review', attempt_state: 'ready_for_review', attempt_outcome: 'done' })
    const wrapper = mountNote([reviewed])
    expect(wrapper.get('.task-origin-status').text()).toBe('Agent says done')
    const button = wrapper.get('button')
    expect(button.text()).toBe('Approve Done')
    expect(button.classes()).toContain('btn-small')
    expect(button.classes()).not.toContain('btn-primary')

    apiPost.mockResolvedValue({ task: { ...reviewed, status: 'done' } })
    apiGet.mockResolvedValue({ workspace: 'personal', tasks: [{ ...reviewed, status: 'done', live_attempt_id: '' }] })
    await button.trigger('click')
    await flushPromises()

    expect(apiPost).toHaveBeenCalledWith('/api/tasks/ship/complete', { workspace: 'personal', expected_revision: REVISION })
    expect(apiGet).toHaveBeenCalledWith('/api/tasks?workspace=personal')
    expect(wrapper.find('button').exists()).toBe(false)
    expect(wrapper.get('.task-origin-status').text()).toBe('Done')
  })

  it('answers a stale-revision refusal with a short line and a reload', async () => {
    const reviewed = task({ status: 'in_review', attempt_state: 'ready_for_review', attempt_outcome: 'done' })
    const wrapper = mountNote([reviewed])
    apiPost.mockRejectedValue(Object.assign(new Error('conflict'), { status: 409 }))
    apiGet.mockResolvedValue({ workspace: 'personal', tasks: [{ ...reviewed, revision: 'b'.repeat(64) }] })
    await wrapper.get('button').trigger('click')
    await flushPromises()

    expect(wrapper.get('[role="alert"]').text()).toBe('This task changed since it was read. Check it and approve again.')
    expect(apiGet).toHaveBeenCalledWith('/api/tasks?workspace=personal')
    // Still reviewable at the newer revision.
    expect(wrapper.get('button').text()).toBe('Approve Done')
  })

  it('offers no Approve when a newer attempt holds the task', () => {
    const wrapper = mountNote([task({
      status: 'in_review', attempt_state: 'ready_for_review', attempt_outcome: 'done',
      attempt_id: 'att-2', live_attempt_id: 'att-2', chat_id: 'chat-2',
    })])
    expect(wrapper.get('.task-origin-status').text()).toBe('A newer attempt holds this task')
    expect(wrapper.find('button').exists()).toBe(false)
  })

  it('reads Done for an approved task', () => {
    const wrapper = mountNote([task({ status: 'done', attempt_state: 'ready_for_review', attempt_id: '', live_attempt_id: '' })])
    expect(wrapper.get('.task-origin-status').text()).toBe('Done')
    expect(wrapper.find('button').exists()).toBe(false)
  })

  it('falls back to the stamp alone when the store does not hold the task', () => {
    const wrapper = mountNote([], CHAT, 'note')
    expect(wrapper.classes()).toContain('task-origin--note')
    expect(wrapper.text()).toContain('This chat works on a task on the board.')
    expect(JSON.parse(wrapper.get('a').attributes('data-to')!)).toEqual({ name: 'task-detail', params: { taskId: 'ship' } })
    expect(wrapper.find('.task-origin-status').exists()).toBe(false)
  })

  it('renders nothing for an ordinary chat', () => {
    const wrapper = mountNote([task({ chat_id: 'other' })], { chat_id: 'chat-1' })
    expect(wrapper.find('.task-origin').exists()).toBe(false)
  })
})
