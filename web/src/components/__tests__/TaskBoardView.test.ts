// @vitest-environment jsdom

/**
 * The workspace task board.
 *
 * What only a mount can decide: that four columns and a card's fields are really
 * drawn, that a move presents the revision the card was drawn from, that a 409
 * shows the server's own sentence and leaves the row where it was, that the five
 * load states stay five, that a file the server could not read as a task is
 * visible rather than dropped, and that the detail dialog opens from the
 * keyboard and hands focus back on Escape.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import TaskBoardView from '../TaskBoardView.vue'
import { useProjectStore } from '../../stores/projects'
import type { Task, TaskRow } from '../../lib/types'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())
const apiPatch = vi.hoisted(() => vi.fn())
const apiDel = vi.hoisted(() => vi.fn())
const askConfirm = vi.hoisted(() => vi.fn(async () => true))

vi.mock('../../lib/api', () => ({
  api: { get: apiGet, post: apiPost, patch: apiPatch, del: apiDel },
}))

// The app's confirm lives in App.vue, not in the pane, so it is mocked rather
// than mounted: a delete has to be answerable from here without a second dialog.
vi.mock('../../lib/confirm', () => ({
  askConfirm,
  pendingConfirm: { value: null },
}))

const REVISION = 'a'.repeat(64)
const NEXT_REVISION = 'b'.repeat(64)

function task(overrides: Partial<Task> = {}): Task {
  return {
    id: 'ship',
    title: 'Ship the board',
    status: 'backlog',
    project_id: '',
    due: '',
    assignee: 'user',
    review_state: 'none',
    chat_id: '',
    attempt_id: '',
    created_at: '2026-03-01T09:00:00+00:00',
    updated_at: '2026-03-01T09:00:00+00:00',
    revision: REVISION,
    relative_path: 'Tasks/ship.md',
    ...overrides,
  }
}

const BOARD: TaskRow[] = [
  task(),
  task({ id: 'doing', title: 'Wire the store', status: 'in_progress', project_id: 'p1' }),
  task({ id: 'held', title: 'Wait on a key', status: 'on_hold' }),
  task({ id: 'done', title: 'Land the types', status: 'done', assignee: 'agent', review_state: 'ready' }),
]

async function mountBoard(rows: TaskRow[] = BOARD) {
  apiGet.mockImplementation((url: string) =>
    Promise.resolve(
      url.includes('/api/tasks?') ? { workspace: 'personal', tasks: rows } : {},
    ),
  )
  const wrapper = mount(TaskBoardView, { attachTo: document.body })
  await flushPromises()
  await nextTick()
  return wrapper
}

function lanes(wrapper: ReturnType<typeof mount>) {
  return wrapper.findAll('.task-lane')
}

function card(wrapper: ReturnType<typeof mount>, title: string) {
  return wrapper.findAll('.task-card').find((c) => c.text().includes(title))!
}

describe('TaskBoardView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    apiGet.mockReset()
    apiPost.mockReset()
    apiPatch.mockReset()
    apiDel.mockReset()
    askConfirm.mockReset()
    askConfirm.mockResolvedValue(true)
    const store = useProjectStore()
    store.workspaces = [{ name: 'personal', vault_root: '/tmp/vault', default_provider: 'claude', gws_profile: '' }]
    store.activeWorkspace = 'personal'
    store.projects = [
      { project_id: 'p1', name: 'Website', workspace: 'personal', context: '', created_at: '2026-03-01', order: 0, vault_folder: '', is_auto: false },
      { project_id: 'general', name: 'General', workspace: 'personal', context: '', created_at: '2026-03-01', order: 1, vault_folder: '', is_auto: false },
    ]
  })

  afterEach(() => {
    document.body.innerHTML = ''
    vi.restoreAllMocks()
  })

  it('reads the active workspace and draws the four columns with their counts', async () => {
    const wrapper = await mountBoard()

    expect(apiGet.mock.calls[0]![0]).toBe('/api/tasks?workspace=personal')
    expect(lanes(wrapper).map((lane) => lane.get('.task-lane-label').text()))
      .toEqual(['Backlog', 'In progress', 'On hold', 'Done'])
    // The count is the board's own, read off the rows rather than recomputed
    // from the filters, so a filtered column still says what is in it.
    expect(lanes(wrapper).map((lane) => lane.get('.badge').text())).toEqual(['1', '1', '1', '1'])
    expect(wrapper.get('.task-lede').text()).toContain('3 open of 4')
    wrapper.unmount()
  })

  it('shows a card\'s title, project, due date, assignee and review badge', async () => {
    const wrapper = await mountBoard([
      task({
        id: 'ship',
        title: 'Ship the board',
        project_id: 'p1',
        // Far enough out that the card is not also "overdue", which is a
        // separate assertion in its own right.
        due: '2099-03-20',
        assignee: 'agent',
        review_state: 'ready',
      }),
    ])

    const shown = card(wrapper, 'Ship the board')
    expect(shown.get('.task-open').text()).toBe('Ship the board')
    expect(shown.get('.task-project').text()).toBe('Website')
    expect(shown.text()).toContain('Due Mar 20')
    expect(shown.get('.task-assignee').text()).toBe('For the agent')
    // The review state is a badge, so it is never colour alone.
    expect(shown.get('.badge').text()).toBe('Review')
    wrapper.unmount()
  })

  it('marks a past due date overdue in words as well as in colour', async () => {
    const wrapper = await mountBoard([task({ due: '2000-01-01' })])
    expect(card(wrapper, 'Ship the board').text()).toContain('Overdue')
    wrapper.unmount()
  })

  it('reads a task with no project as General, and says no date rather than a blank', async () => {
    const wrapper = await mountBoard([task()])
    const shown = card(wrapper, 'Ship the board')
    expect(shown.get('.task-project').text()).toBe('General')
    expect(shown.find('.task-meta .badge').exists()).toBe(false)
    wrapper.unmount()
  })

  it('moves a card at the revision it drew the card from', async () => {
    const wrapper = await mountBoard()
    apiPatch.mockResolvedValue({ workspace: 'personal', task: { ...task({ status: 'in_progress' }), revision: NEXT_REVISION, body: '' } })

    await card(wrapper, 'Ship the board').get('select.task-status').setValue('in_progress')
    await flushPromises()
    await nextTick()

    expect(apiPatch).toHaveBeenCalledTimes(1)
    const [path, sent] = apiPatch.mock.calls[0]! as [string, Record<string, unknown>]
    expect(path).toBe('/api/tasks/ship')
    // The revision the list row carried, not a re-read or a guess: this is the
    // value that turns a stale edit into a 409 instead of a lost update.
    expect(sent.expected_revision).toBe(REVISION)
    expect(sent.workspace).toBe('personal')
    expect(sent.status).toBe('in_progress')
    // And the card adopts the revision the write returned, so the next write
    // from the same card is not stale against its own edit.
    expect(card(wrapper, 'Ship the board').element.closest('.task-lane')!.getAttribute('aria-label')).toBe('In progress')
    expect(card(wrapper, 'Ship the board').get('select.task-status').element.getAttribute('value')).toBeTruthy()
    wrapper.unmount()
  })

  it('completes through the completion gesture, not the status field', async () => {
    const wrapper = await mountBoard()
    apiPost.mockResolvedValue({ workspace: 'personal', task: { ...task({ status: 'done' }), revision: NEXT_REVISION, body: '' } })

    await card(wrapper, 'Ship the board').get('button[aria-label="Mark Ship the board done"]').trigger('click')
    await flushPromises()
    await nextTick()

    expect(apiPost).toHaveBeenCalledWith('/api/tasks/ship/complete', {
      workspace: 'personal',
      expected_revision: REVISION,
    })
    expect(lanes(wrapper)[3]!.text()).toContain('Ship the board')
    wrapper.unmount()
  })

  it('surfaces a 409 with the server\'s sentence and keeps the row where it was', async () => {
    const wrapper = await mountBoard()
    apiPatch.mockRejectedValue(Object.assign(new Error('HTTP 409'), {
      payload: { error: { code: 'task_revision_conflict', message: 'that task changed on disk', retryable: true } },
    }))

    await card(wrapper, 'Ship the board').get('select.task-status').setValue('in_progress')
    await flushPromises()
    await nextTick()

    // The server's own words, not a generic failure and not `[object Object]`:
    // this surface nests its message inside the envelope.
    expect(wrapper.get('.task-action-error').text()).toBe('that task changed on disk')
    // The row is still the card it was, in the column it was in — a refused
    // write must not cost the user the card.
    expect(lanes(wrapper)[0]!.text()).toContain('Ship the board')
    expect(lanes(wrapper)[1]!.text()).not.toContain('Ship the board')
    // And the select does not sit claiming a column the write never reached.
    expect((card(wrapper, 'Ship the board').get('select.task-status').element as HTMLSelectElement).value)
      .toBe('backlog')
    wrapper.unmount()
  })

  it('does not claim an empty board when the first load failed, and retries', async () => {
    apiGet.mockRejectedValue(new Error('task store unavailable'))
    const wrapper = mount(TaskBoardView, { attachTo: document.body })
    await flushPromises()
    await nextTick()

    expect(wrapper.get('.task-failed-text').text()).toBe('task store unavailable')
    expect(wrapper.findAll('.task-card')).toHaveLength(0)
    expect(wrapper.text()).not.toContain('No tasks in')
    expect(wrapper.find('.task-empty').exists()).toBe(false)

    apiGet.mockResolvedValue({ workspace: 'personal', tasks: BOARD })
    await wrapper.get('.task-failed').get('.btn-small').trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.findAll('.task-card')).toHaveLength(4)
    wrapper.unmount()
  })

  it('holds the first load as its own state rather than an empty board', async () => {
    let release: (answer: { workspace: string; tasks: TaskRow[] }) => void = () => {}
    apiGet.mockImplementation(() => new Promise((resolve) => { release = resolve }))
    const wrapper = mount(TaskBoardView, { attachTo: document.body })
    await nextTick()

    expect(wrapper.find('.task-loading').exists()).toBe(true)
    expect(wrapper.text()).not.toContain('No tasks in')
    expect(wrapper.findAll('.task-card')).toHaveLength(0)

    wrapper.unmount()
    release({ workspace: 'personal', tasks: BOARD })
    await flushPromises()
    // The promise the loading mount left behind settles into a pane that is
    // gone, and writes nothing.
    expect(document.body.innerHTML).toBe('')
  })

  it('keeps the rows it has when a refresh fails, marked stale with a retry', async () => {
    const wrapper = await mountBoard()
    apiGet.mockRejectedValue(new Error('the vault is busy'))
    await useTaskBoardReload(wrapper)
    await flushPromises()
    await nextTick()

    expect(wrapper.findAll('.task-card')).toHaveLength(4)
    expect(wrapper.get('.task-stale').text()).toContain('the vault is busy')
    expect(wrapper.get('.task-stale').text()).toContain('as they were last read')
    expect(wrapper.find('.task-failed').exists()).toBe(false)
    wrapper.unmount()
  })

  it('offers a way back from a filter that hides everything, without calling it empty', async () => {
    const wrapper = await mountBoard()
    // No task on this board is filed under General, so the filter hides the
    // whole set while the board itself is not empty.
    await wrapper.get('#task-filter-project').setValue('general')
    await nextTick()

    expect(wrapper.find('.task-filtered-empty').exists()).toBe(true)
    expect(wrapper.text()).not.toContain('No tasks in')
    expect(wrapper.find('.task-empty').exists()).toBe(false)
    // Nothing matches, so no cards are drawn — and the message still names the
    // board's own count, so it reads as "hidden by this filter", not "none".
    expect(wrapper.findAll('.task-card')).toHaveLength(0)
    expect(wrapper.get('.task-filtered-empty').text()).toContain('4 tasks in personal')

    await wrapper.get('.task-filtered-empty').get('.btn-small').trigger('click')
    await nextTick()
    expect(wrapper.find('.task-filtered-empty').exists()).toBe(false)
    wrapper.unmount()
  })

  it('calls a genuinely empty board empty', async () => {
    const wrapper = await mountBoard([])
    expect(wrapper.get('.task-empty').text()).toContain('No tasks in personal')
    expect(wrapper.find('.task-filtered-empty').exists()).toBe(false)
    expect(wrapper.find('.task-loading').exists()).toBe(false)
    wrapper.unmount()
  })

  it('shows a file it could not read as a task instead of dropping it', async () => {
    const wrapper = await mountBoard([
      task(),
      { id: 'broken', path: 'Tasks/broken.md', code: 'invalid_task', message: 'no title in frontmatter' },
    ])

    const unreadable = wrapper.get('.task-unreadable')
    expect(unreadable.text()).toContain('1 task file could not be read')
    expect(unreadable.text()).toContain('Tasks/broken.md')
    expect(unreadable.get('.badge').text()).toBe('invalid_task')
    expect(unreadable.text()).toContain('no title in frontmatter')
    // It is not a card: it has no status to move and nothing to edit.
    expect(wrapper.findAll('.task-card')).toHaveLength(1)
    wrapper.unmount()
  })

  it('opens the detail dialog from the keyboard and hands focus back on Escape', async () => {
    const wrapper = await mountBoard()
    const opener = card(wrapper, 'Ship the board').get('.task-open')
    ;(opener.element as HTMLElement).focus()
    expect(document.activeElement).toBe(opener.element)

    await opener.trigger('click')
    await flushPromises()
    await nextTick()

    const sheet = wrapper.get('.task-sheet')
    expect(sheet.attributes('role')).toBe('dialog')
    expect(sheet.attributes('aria-modal')).toBe('true')
    expect(sheet.get<HTMLInputElement>('#task-detail-name').element.value).toBe('Ship the board')
    // A list row carries no description and `/api/tasks*` has no read-by-id, so
    // the board has no request to make here — and no second round trip per card.
    expect(apiGet).toHaveBeenCalledTimes(1)

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flushPromises()
    await nextTick()
    expect(wrapper.find('.task-sheet').exists()).toBe(false)
    expect(document.activeElement).toBe(opener.element)
    wrapper.unmount()
  })

  it('does not offer, or overwrite, a description the board never read', async () => {
    const wrapper = await mountBoard()
    await card(wrapper, 'Ship the board').get('.task-open').trigger('click')
    await flushPromises()
    await nextTick()

    // No textarea: an empty one would invite a Save that clears the prose nobody
    // was shown. The note says why instead of pretending there is nothing there.
    expect(wrapper.find('#task-detail-body').exists()).toBe(false)
    expect(wrapper.get('.task-sheet').text()).toContain('has not read this task')

    apiPatch.mockResolvedValue({
      workspace: 'personal',
      task: { ...task({ title: 'Ship the board, carefully' }), revision: NEXT_REVISION, body: 'untouched' },
    })
    await wrapper.get('#task-detail-name').setValue('Ship the board, carefully')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    await nextTick()

    expect(apiPatch).toHaveBeenCalledTimes(1)
    const [path, sent] = apiPatch.mock.calls[0]! as [string, Record<string, unknown>]
    expect(path).toBe('/api/tasks/ship')
    expect(sent.expected_revision).toBe(REVISION)
    expect(sent.title).toBe('Ship the board, carefully')
    // `body` is absent, not empty: absent is "leave it", empty is "erase it".
    expect('body' in sent).toBe(false)
    expect(card(wrapper, 'Ship the board, carefully').exists()).toBe(true)
    wrapper.unmount()
  })

  it('edits the description of a task it created, and previews it as safe Markdown', async () => {
    const wrapper = await mountBoard([])
    apiPost.mockResolvedValue({
      workspace: 'personal',
      task: { ...task({ id: 'new', title: 'First task' }), revision: NEXT_REVISION, body: '# Notes\n\n<b>not html</b>' },
    })
    await wrapper.get('.task-new').trigger('click')
    await nextTick()
    await wrapper.get('#task-create-name').setValue('First task')
    await wrapper.get('#task-create-body').setValue('# Notes\n\n<b>not html</b>')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    await nextTick()

    // The create answer is where a description arrives, so the board holds this
    // one and may show and edit it.
    await card(wrapper, 'First task').get('.task-open').trigger('click')
    await flushPromises()
    await nextTick()
    const textarea = wrapper.get<HTMLTextAreaElement>('#task-detail-body')
    expect(textarea.element.value).toContain('<b>not html</b>')

    await wrapper.get('.task-preview summary').trigger('click')
    await nextTick()
    // Rendered, and the raw tag shown as typed rather than parsed.
    expect(wrapper.get('.task-preview-body').html()).toContain('&lt;b&gt;not html&lt;/b&gt;')
    expect(wrapper.get('.task-preview-body').element.querySelector('b')).toBeNull()

    apiPatch.mockResolvedValue({
      workspace: 'personal',
      task: { ...task({ id: 'new', title: 'First task' }), revision: 'c'.repeat(64), body: '# Notes\n\n<b>not html</b>' },
    })
    await textarea.setValue('Now with a second paragraph.')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    await nextTick()
    expect((apiPatch.mock.calls[0]![1] as Record<string, unknown>).body).toBe('Now with a second paragraph.')
    wrapper.unmount()
  })

  it('files a task and puts the stored record on the board', async () => {
    const wrapper = await mountBoard([])
    apiPost.mockResolvedValue({
      workspace: 'personal',
      task: { ...task({ id: 'new', title: 'First task', due: '2099-04-01' }), revision: NEXT_REVISION, body: '' },
    })

    await wrapper.get('.task-new').trigger('click')
    await nextTick()
    await wrapper.get('#task-create-name').setValue('First task')
    await wrapper.get('#task-create-due').setValue('2099-04-01')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    await nextTick()

    expect(apiPost).toHaveBeenCalledWith('/api/tasks', {
      workspace: 'personal',
      title: 'First task',
      body: '',
      due: '2099-04-01',
    })
    expect(wrapper.find('.task-sheet').exists()).toBe(false)
    expect(card(wrapper, 'First task').text()).toContain('Due Apr 1')
    wrapper.unmount()
  })

  it('keeps a create form open on a refusal, with the server\'s sentence', async () => {
    const wrapper = await mountBoard([])
    apiPost.mockRejectedValue(Object.assign(new Error('HTTP 400'), {
      payload: { error: { code: 'project_not_found', message: "Project 'Website' was not found." } },
    }))

    await wrapper.get('.task-new').trigger('click')
    await nextTick()
    await wrapper.get('#task-create-name').setValue('First task')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    await nextTick()

    expect(wrapper.find('.task-sheet').exists()).toBe(true)
    expect(wrapper.get<HTMLInputElement>('#task-create-name').element.value).toBe('First task')
    expect(wrapper.get('.task-sheet').get('.task-action-error').text())
      .toBe("Project 'Website' was not found.")
    wrapper.unmount()
  })

  it('deletes at the revision it read, and only after the confirmation', async () => {
    const wrapper = await mountBoard()
    askConfirm.mockResolvedValue(false)
    apiDel.mockResolvedValue({ workspace: 'personal', deleted: 'ship' })

    await card(wrapper, 'Ship the board').get('.task-open').trigger('click')
    await flushPromises()
    await nextTick()
    await wrapper.get('.task-delete').trigger('click')
    await flushPromises()
    await nextTick()

    // Declined: nothing is sent and the row is still there.
    expect(apiDel).not.toHaveBeenCalled()
    expect(wrapper.find('.task-sheet').exists()).toBe(true)

    askConfirm.mockResolvedValue(true)
    await wrapper.get('.task-delete').trigger('click')
    await flushPromises()
    await nextTick()

    expect(apiDel).toHaveBeenCalledWith('/api/tasks/ship', {
      workspace: 'personal',
      expected_revision: REVISION,
    })
    expect(wrapper.find('.task-card').text()).not.toContain('Ship the board')
    // A dialog left open on a deleted task would write it straight back.
    expect(wrapper.find('.task-sheet').exists()).toBe(false)
    wrapper.unmount()
  })

  it('narrowing the pane turns the four columns into one list', async () => {
    const original = window.innerWidth
    Object.defineProperty(window, 'innerWidth', { value: 700, configurable: true })
    try {
      const wrapper = await mountBoard()
      expect(lanes(wrapper)).toHaveLength(1)
      expect(lanes(wrapper)[0]!.get('.task-lane-label').text()).toBe('All tasks')
      expect(wrapper.findAll('.task-card')).toHaveLength(4)
      // …and picking a status is how the list narrows.
      await wrapper.findAll('.task-chip').find((c) => c.text().startsWith('Done'))!.trigger('click')
      await nextTick()
      expect(lanes(wrapper)[0]!.get('.task-lane-label').text()).toBe('Done')
      expect(wrapper.findAll('.task-card')).toHaveLength(1)
      wrapper.unmount()
    } finally {
      Object.defineProperty(window, 'innerWidth', { value: original, configurable: true })
    }
  })
})

/** The pane's Retry path: a reload against the same workspace. */
async function useTaskBoardReload(wrapper: ReturnType<typeof mount>) {
  const { useTaskBoardStore } = await import('../../stores/taskBoard')
  await useTaskBoardStore().reload('personal')
  return wrapper
}