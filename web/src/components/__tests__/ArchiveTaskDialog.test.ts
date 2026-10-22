// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import ArchiveTaskDialog from '../ArchiveTaskDialog.vue'
import { askArchiveTask, pendingArchiveTask } from '../../lib/archiveTaskChoice'
import { useTaskSignalsStore } from '../../stores/taskSignals'
import type { Task } from '../../lib/types'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())
vi.mock('../../lib/api', () => ({ api: { get: apiGet, post: apiPost } }))

const REVISION = 'a'.repeat(64)
const FRESH = 'b'.repeat(64)

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
    linked_chats: [],
    created_at: '',
    updated_at: '',
    revision: REVISION,
    relative_path: 'Tasks/ship.md',
    attempt_state: 'running',
    attempt_outcome: '',
    attempt_summary: '',
    attempt_detail: '',
    live_attempt_id: 'att-1',
    completed_at: null,
    has_resolution: false,
    ...overrides,
  }
}

let wrapper: ReturnType<typeof mount> | null = null

function find<T extends Element>(selector: string): T {
  const element = document.querySelector<T>(selector)
  if (!element) throw new Error(`Missing dialog element: ${selector}`)
  return element
}

function button(label: string): HTMLButtonElement {
  const found = [...document.querySelectorAll<HTMLButtonElement>('button')]
    .find(b => b.textContent?.trim() === label)
  if (!found) throw new Error(`Missing button: ${label}`)
  return found
}

async function settle() {
  await nextTick()
  await nextTick()
  await new Promise<void>(resolve => setTimeout(resolve, 0))
}

async function open(t: Task = task(), awaitingReview = false) {
  const answer = askArchiveTask(t, 'personal', awaitingReview)
  await settle()
  // Boxed: returning the promise from an async function would await it here.
  return { answer }
}

async function typeNote(text: string) {
  const field = find<HTMLTextAreaElement>('#archive-task-resolution')
  field.value = text
  field.dispatchEvent(new Event('input', { bubbles: true }))
  await settle()
}

describe('ArchiveTaskDialog', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    apiGet.mockReset()
    apiGet.mockResolvedValue({ tasks: [] })
    apiPost.mockReset()
    apiPost.mockResolvedValue({})
    useTaskSignalsStore().loadedWorkspace = 'personal'
    wrapper = mount(ArchiveTaskDialog, { attachTo: document.body })
  })

  afterEach(() => {
    pendingArchiveTask.value?.resolve('cancel')
    wrapper?.unmount()
    wrapper = null
  })

  it('offers Cancel, Keep task open and Complete task… naming the task', async () => {
    const { answer } = await open()
    expect(find('.confirm-title').textContent?.trim()).toBe('Archive chat')
    expect(find('.confirm-message').textContent).toContain('"Ship the board"')
    button('Keep task open')
    button('Complete task…')
    expect(document.activeElement).toBe(button('Cancel'))
    button('Cancel').click()
    await expect(answer).resolves.toBe('cancel')
  })

  it('Keep task open resolves keep and calls no API', async () => {
    const { answer } = await open()
    button('Keep task open').click()
    await expect(answer).resolves.toBe('keep')
    expect(apiPost).not.toHaveBeenCalled()
  })

  it('Cancel and Escape resolve cancel', async () => {
    const { answer: first } = await open()
    button('Cancel').click()
    await expect(first).resolves.toBe('cancel')

    const { answer: second } = await open()
    button('Cancel').dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))
    await expect(second).resolves.toBe('cancel')
  })

  it('Complete task… then Mark done detaches, completes with the note, reloads, and resolves marked-done', async () => {
    apiPost.mockResolvedValueOnce({ task: { revision: FRESH } })
    const { answer } = await open()
    button('Complete task…').click()
    await settle()
    expect(document.activeElement).toBe(find('#archive-task-resolution'))
    expect(button('Mark done').disabled).toBe(false)
    await typeNote('  Merged  ')
    button('Mark done').click()
    await expect(answer).resolves.toBe('marked-done')
    expect(apiPost).toHaveBeenNthCalledWith(1, '/api/tasks/ship/attempt/att-1/detach', { workspace: 'personal' })
    expect(apiPost).toHaveBeenNthCalledWith(2, '/api/tasks/ship/complete', {
      workspace: 'personal',
      expected_revision: FRESH,
      resolution: 'Merged',
    })
    expect(apiGet).toHaveBeenCalled()
  })

  it('a task awaiting review offers Approve Done and resolves approved after one complete call', async () => {
    const { answer } = await open(task({ status: 'in_review', attempt_state: 'ready_for_review' }), true)
    expect(find('.confirm-message').textContent).toContain('waiting for your review')
    button('Complete task…').click()
    await settle()
    await typeNote('Looks good')
    button('Approve Done').click()
    await expect(answer).resolves.toBe('approved')
    expect(apiPost).toHaveBeenCalledTimes(1)
    expect(apiPost).toHaveBeenCalledWith('/api/tasks/ship/complete', {
      workspace: 'personal',
      expected_revision: REVISION,
      resolution: 'Looks good',
    })
  })

  it('a blank note completes with an empty resolution', async () => {
    const { answer } = await open(task({ attempt_id: '', live_attempt_id: '' }))
    button('Complete task…').click()
    await settle()
    button('Mark done').click()
    await expect(answer).resolves.toBe('marked-done')
    expect(apiPost).toHaveBeenCalledTimes(1)
    expect(apiPost).toHaveBeenCalledWith('/api/tasks/ship/complete', {
      workspace: 'personal',
      expected_revision: REVISION,
      resolution: '',
    })
  })

  it('the note step offers Back and one completion action, and no no-note button', async () => {
    await open()
    button('Complete task…').click()
    await settle()
    const dialog = find<HTMLElement>('[role="dialog"]')
    expect([...dialog.querySelectorAll('button')].map(b => b.textContent?.trim())).toEqual(['Back', 'Mark done'])
    expect(dialog.textContent).not.toContain('Complete without note')
  })

  it('a 409 shows the refusal and keeps the dialog open', async () => {
    apiPost.mockReset()
    apiPost.mockRejectedValue({ status: 409 })
    const { answer } = await open(task({ attempt_id: '', live_attempt_id: '' }))
    let settled = false
    void answer.then(() => { settled = true })
    button('Complete task…').click()
    await settle()
    await typeNote('Done')
    button('Mark done').click()
    await settle()
    expect(find('[role="alert"]').textContent).toBe('This task changed since it was read. Check it and try again.')
    expect(settled).toBe(false)
    expect(pendingArchiveTask.value).not.toBeNull()
    expect(button('Mark done').disabled).toBe(false)
  })

  it('puts focus back on the note after a refused save', async () => {
    apiPost.mockReset()
    apiPost.mockRejectedValue({ status: 409 })
    await open(task({ attempt_id: '', live_attempt_id: '' }))
    button('Complete task…').click()
    await settle()
    await typeNote('Done')
    button('Mark done').click()
    await settle()
    expect(document.activeElement).toBe(find('#archive-task-resolution'))
  })

  it('ignores Escape and Cancel while a save is in flight', async () => {
    let release: (v: unknown) => void = () => {}
    apiPost.mockReset()
    apiPost.mockReturnValue(new Promise(r => { release = r }))
    const { answer } = await open(task({ attempt_id: '', live_attempt_id: '' }))
    let settled = false
    void answer.then(() => { settled = true })
    button('Complete task…').click()
    await settle()
    await typeNote('Done')
    button('Mark done').click()
    await settle()
    find('[role="dialog"]').dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))
    find<HTMLElement>('.confirm-backdrop').click()
    button('Back').click()
    await settle()
    expect(settled).toBe(false)
    expect(pendingArchiveTask.value).not.toBeNull()
    release({})
    await expect(answer).resolves.toBe('marked-done')
  })

  it('Back returns to the choice', async () => {
    const { answer } = await open()
    button('Complete task…').click()
    await settle()
    button('Back').click()
    await settle()
    button('Keep task open')
    expect(document.querySelector('#archive-task-resolution')).toBeNull()
    button('Cancel').click()
    await expect(answer).resolves.toBe('cancel')
  })
})
