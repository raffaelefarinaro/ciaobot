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
import { useTaskBoardStore } from '../../stores/taskBoard'
import type { Task, TaskRow } from '../../lib/types'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())
const apiPatch = vi.hoisted(() => vi.fn())
const apiDel = vi.hoisted(() => vi.fn())
const askConfirm = vi.hoisted(() => vi.fn(
  async (_message?: string, _options?: { title?: string }) => true,
))
const pendingConfirm = vi.hoisted(() => ({ value: null as unknown }))

vi.mock('../../lib/api', () => ({
  api: { get: apiGet, post: apiPost, patch: apiPatch, del: apiDel },
}))

// The app's confirm lives in App.vue, not in the pane, so it is mocked rather
// than mounted: a delete has to be answerable from here without a second dialog.
// `pendingConfirm` is a real ref-shaped slot because the pane reads it to decide
// whether its own Escape handling applies while a confirm is up.
vi.mock('../../lib/confirm', () => ({
  askConfirm,
  pendingConfirm,
}))

const REVISION = 'a'.repeat(64)
const NEXT_REVISION = 'b'.repeat(64)
const THIRD_REVISION = 'c'.repeat(64)

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
    attempt_state: '',
    live_attempt_id: '',
    changed_since_delegated: false,
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

/** A `GET /api/tasks/{id}` answer for `task`, with a real `body`. */
function detailAnswer(task: Task, body: string) {
  return { workspace: 'personal', task: { ...task, revision: NEXT_REVISION, body } }
}

function lanes(wrapper: ReturnType<typeof mount>) {
  return wrapper.findAll('.task-lane')
}

function card(wrapper: ReturnType<typeof mount>, title: string) {
  return wrapper.findAll('.task-card').find((c) => c.text().includes(title))!
}

describe('TaskBoardView', () => {
  /** Reports the pane's inline size, which is what `NARROW_PANE_PX` reads. */
  let reportPaneWidth = (_width: number) => {}

  beforeEach(() => {
    setActivePinia(createPinia())
    apiGet.mockReset()
    apiPost.mockReset()
    apiPatch.mockReset()
    apiDel.mockReset()
    askConfirm.mockReset()
    askConfirm.mockResolvedValue(true)
    pendingConfirm.value = null
    // jsdom has no ResizeObserver and no layout, so the pane's width is driven
    // from here: the component reads the same `contentRect.width` a real
    // observer would hand it.
    vi.stubGlobal('ResizeObserver', class {
      private cb: ResizeObserverCallback
      constructor(cb: ResizeObserverCallback) { this.cb = cb }
      observe(_target: Element) {
        reportPaneWidth = (width: number) => {
          this.cb([{ contentRect: { width } } as unknown as ResizeObserverEntry],
            this as unknown as ResizeObserver)
        }
      }
      unobserve() {}
      disconnect() { reportPaneWidth = () => {} }
    })
    const store = useProjectStore()
    store.workspaces = [
      { name: 'personal', vault_root: '/tmp/vault', default_provider: 'claude', gws_profile: '' },
      { name: 'work', vault_root: '/tmp/work', default_provider: 'claude', gws_profile: '' },
    ]
    store.activeWorkspace = 'personal'
    store.projects = [
      { project_id: 'p1', name: 'Website', workspace: 'personal', context: '', created_at: '2026-03-01', order: 0, vault_folder: '', is_auto: false },
      { project_id: 'general', name: 'General', workspace: 'personal', context: '', created_at: '2026-03-01', order: 1, vault_folder: '', is_auto: false },
    ]
  })

  afterEach(() => {
    document.body.innerHTML = ''
    vi.unstubAllGlobals()
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
    expect(wrapper.get('.task-action-error').text()).toContain('that task changed on disk')
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
    apiGet.mockImplementation((url: string) => Promise.resolve(
      url.includes('/api/tasks?')
        ? { workspace: 'personal', tasks: BOARD }
        : detailAnswer(task(), 'Prose the list never carried.'),
    ))
    const wrapper = mount(TaskBoardView, { attachTo: document.body })
    await flushPromises()
    await nextTick()

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
    // The description the list row does not carry is read by id, at the
    // workspace being drawn.
    expect(apiGet.mock.calls.map((call) => call[0])).toContain(
      '/api/tasks/ship?workspace=personal',
    )

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flushPromises()
    await nextTick()
    expect(wrapper.find('.task-sheet').exists()).toBe(false)
    expect(document.activeElement).toBe(opener.element)
    wrapper.unmount()
  })

  it('never offers, or overwrites, a description it could not read', async () => {
    const wrapper = await mountBoard()
    apiGet.mockRejectedValue(Object.assign(new Error('HTTP 404'), {
      payload: { error: { code: 'task_not_found', message: 'that task is gone' } },
    }))
    await card(wrapper, 'Ship the board').get('.task-open').trigger('click')
    await flushPromises()
    await nextTick()

    // No textarea: an empty one would invite a Save that clears the prose nobody
    // was shown. The note says why instead of pretending there is nothing there,
    // and offers the way back.
    expect(wrapper.find('#task-detail-body').exists()).toBe(false)
    expect(wrapper.get('.task-sheet').text()).toContain('could not read this task')
    expect(wrapper.get('.task-sheet').text()).toContain('that task is gone')

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

  it('sends only the fields the dialog changed, and nothing at all when none did', async () => {
    const wrapper = await mountBoard()
    apiGet.mockResolvedValue(detailAnswer(task({ project_id: 'p1' }), 'Kept.'))
    await card(wrapper, 'Ship the board').get('.task-open').trigger('click')
    await flushPromises()
    await nextTick()

    // Save with nothing touched is not a write. A PATCH with every field resent
    // would be one — and `project_id: 'p1'` alone is enough to 400 a task whose
    // project has since been deleted out from under the board.
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    await nextTick()
    expect(apiPatch).not.toHaveBeenCalled()

    await wrapper.get('#task-detail-name').setValue('Ship the board, carefully')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    await nextTick()

    const [path, sent] = apiPatch.mock.calls[0]! as [string, Record<string, unknown>]
    expect(path).toBe('/api/tasks/ship')
    // The read is the newest thing this board knows, so its revision is the one
    // the write presents.
    expect(sent.expected_revision).toBe(NEXT_REVISION)
    expect(sent.title).toBe('Ship the board, carefully')
    // The unchanged fields are absent, so an untouched `project_id` is never
    // resolved and can never 400 on a project the workspace no longer has.
    expect('project_id' in sent).toBe(false)
    expect('due' in sent).toBe(false)
    expect('assignee' in sent).toBe(false)
    // The description was read and not edited, so it is not rewritten at all.
    expect('body' in sent).toBe(false)
    wrapper.unmount()
  })

  it('re-reads the description on Retry and then offers it', async () => {
    const wrapper = await mountBoard()
    apiGet.mockRejectedValueOnce(Object.assign(new Error('HTTP 500'), {
      payload: { error: { code: 'task_read_failed', message: 'the file is locked' } },
    }))
    await card(wrapper, 'Ship the board').get('.task-open').trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.find('#task-detail-body').exists()).toBe(false)

    apiGet.mockResolvedValue(detailAnswer(task(), '# Notes\n\n<b>not html</b>'))
    await wrapper.findAll('.task-chip').find((c) => c.text() === 'Retry')!.trigger('click')
    await flushPromises()
    await nextTick()

    const textarea = wrapper.get<HTMLTextAreaElement>('#task-detail-body')
    expect(textarea.element.value).toContain('<b>not html</b>')

    await wrapper.get('.task-preview summary').trigger('click')
    await nextTick()
    // Rendered, and the raw tag shown as typed rather than parsed.
    expect(wrapper.get('.task-preview-body').html()).toContain('&lt;b&gt;not html&lt;/b&gt;')
    expect(wrapper.get('.task-preview-body').element.querySelector('b')).toBeNull()
    wrapper.unmount()
  })

  // A `described` slot outlives the dialog and the pane, so "the board already
  // has this body" is a claim about an instant that has passed. Reusing it without
  // reading is how another writer's prose gets written back over at their own
  // revision: the write succeeds, so there is no 409 to notice it by.
  it('re-reads on reopen, so prose another writer has since replaced is never written back', async () => {
    const wrapper = await mountBoard([task()])
    apiGet.mockResolvedValue(detailAnswer(task(), 'OLD'))
    await card(wrapper, 'Ship the board').get('.task-open').trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.get<HTMLTextAreaElement>('#task-detail-body').element.value).toBe('OLD')
    await wrapper.get('.task-sheet-head .btn-icon').trigger('click')
    await nextTick()

    // Another writer edits the body. The board re-reads and reports the new
    // revision — a list row carries no body, so nothing on the board has theirs.
    const theirs = task({ revision: THIRD_REVISION })
    let releaseRead: (answer: unknown) => void = () => {}
    apiGet.mockImplementation((url: string) => (url.includes('/api/tasks?')
      ? Promise.resolve({ workspace: 'personal', tasks: [theirs] })
      : new Promise((resolve) => { releaseRead = resolve })))
    await useTaskBoardReload(wrapper)
    await flushPromises()
    await nextTick()

    // Reopening reads again, even though the slot still holds a body for this
    // task: the slot's revision is not the row's any more.
    await card(wrapper, 'Ship the board').get('.task-open').trigger('click')
    await nextTick()
    expect(apiGet.mock.calls.map((call) => call[0])).toContain('/api/tasks/ship?workspace=personal')
    releaseRead({ workspace: 'personal', task: { ...theirs, body: 'THEIR PROSE' } })
    await flushPromises()
    await nextTick()

    // What the dialog shows is what they wrote.
    const textarea = wrapper.get<HTMLTextAreaElement>('#task-detail-body')
    expect(textarea.element.value).toBe('THEIR PROSE')

    apiPatch.mockResolvedValue({
      workspace: 'personal',
      task: { ...theirs, revision: 'd'.repeat(64), body: 'THEIR PROSE\n\nMINE' },
    })
    await textarea.setValue('THEIR PROSE\n\nMINE')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    await nextTick()

    const sent = apiPatch.mock.calls[0]![1] as Record<string, unknown>
    expect(sent.expected_revision).toBe(THIRD_REVISION)
    // Built on the prose that was on screen. The one this board used to hold would
    // go out at the same revision and overwrite their edit with no 409.
    expect(sent.body).toBe('THEIR PROSE\n\nMINE')
    expect(sent.body).not.toContain('OLD')
    wrapper.unmount()
  })

  it('prefills a description it already holds, so a Save before the read lands writes no body', async () => {
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

    // The create answer carried the record as stored, so the board holds this
    // description at the row's own revision and the form is filled the moment the
    // dialog opens — with the read that will confirm it still in flight.
    let releaseRead: (answer: unknown) => void = () => {}
    apiGet.mockImplementation((url: string) => url.includes('/api/tasks?')
      ? Promise.resolve({ workspace: 'personal', tasks: [] })
      : new Promise((resolve) => { releaseRead = resolve }))
    await card(wrapper, 'First task').get('.task-open').trigger('click')
    await nextTick()
    expect(apiGet.mock.calls.map((call) => call[0])).toContain('/api/tasks/new?workspace=personal')

    apiPatch.mockResolvedValue({
      workspace: 'personal',
      task: { ...task({ id: 'new', title: 'First task, carefully' }), revision: THIRD_REVISION, body: '# Notes\n\n<b>not html</b>' },
    })
    await wrapper.get('#task-detail-name').setValue('First task, carefully')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    await nextTick()

    // A Save inside that window writes the title alone. An unfilled form would be
    // `''` against prose the board does hold, and that difference is a `body: ''`
    // — the description the user just wrote, deleted by the first Save.
    const sent = apiPatch.mock.calls[0]![1] as Record<string, unknown>
    expect(sent.title).toBe('First task, carefully')
    expect('body' in sent).toBe(false)
    expect(sent.expected_revision).toBe(NEXT_REVISION)

    // The read answers for the record as it was before that Save, so the board
    // is newer: the read is dropped rather than refilling the form with prose the
    // write has already superseded, and the description the Save left in place
    // stays on screen.
    releaseRead(detailAnswer(task({ id: 'new', title: 'First task' }), '# Notes\n\n<b>not html</b>'))
    await flushPromises()
    await nextTick()
    const textarea = wrapper.get<HTMLTextAreaElement>('#task-detail-body')
    expect(textarea.element.value).toContain('<b>not html</b>')
    expect(wrapper.get('.task-sheet').text()).not.toContain('could not read')
    wrapper.unmount()
  })

  it('moves the dialog\'s status without completing the task', async () => {
    const wrapper = await mountBoard()
    apiGet.mockResolvedValue(detailAnswer(task(), ''))
    apiPatch.mockResolvedValue({
      workspace: 'personal',
      task: { ...task({ status: 'on_hold' }), revision: NEXT_REVISION, body: '' },
    })
    await card(wrapper, 'Ship the board').get('.task-open').trigger('click')
    await flushPromises()
    await nextTick()

    await wrapper.get('#task-detail-status').setValue('on_hold')
    await flushPromises()
    await nextTick()

    // "On hold" is a move, not the completion gesture: the record must not be
    // marked done because the select changed.
    expect(apiPost).not.toHaveBeenCalled()
    expect(apiPatch).toHaveBeenCalledTimes(1)
    const [path, sent] = apiPatch.mock.calls[0]! as [string, Record<string, unknown>]
    expect(path).toBe('/api/tasks/ship')
    expect(sent.status).toBe('on_hold')
    expect(sent.expected_revision).toBe(NEXT_REVISION)
    // And the select follows the stored record rather than snapping back to the
    // status the dialog was opened on.
    expect((wrapper.get('#task-detail-status').element as HTMLSelectElement).value).toBe('on_hold')
    expect(lanes(wrapper)[2]!.text()).toContain('Ship the board')
    wrapper.unmount()
  })

  it('completes from the dialog when Done is the status chosen', async () => {
    const wrapper = await mountBoard()
    apiGet.mockResolvedValue(detailAnswer(task(), ''))
    apiPost.mockResolvedValue({
      workspace: 'personal',
      task: { ...task({ status: 'done' }), revision: NEXT_REVISION, body: '' },
    })
    await card(wrapper, 'Ship the board').get('.task-open').trigger('click')
    await flushPromises()
    await nextTick()

    await wrapper.get('#task-detail-status').setValue('done')
    await flushPromises()
    await nextTick()

    expect(apiPatch).not.toHaveBeenCalled()
    expect(apiPost).toHaveBeenCalledWith('/api/tasks/ship/complete', {
      workspace: 'personal',
      expected_revision: NEXT_REVISION,
    })
    expect((wrapper.get('#task-detail-status').element as HTMLSelectElement).value).toBe('done')
    wrapper.unmount()
  })

  it('leaves Escape to the delete confirm instead of closing the dialog under it', async () => {
    const wrapper = await mountBoard()
    apiGet.mockResolvedValue(detailAnswer(task(), ''))
    await card(wrapper, 'Ship the board').get('.task-open').trigger('click')
    await flushPromises()
    await nextTick()

    let release: (ok: boolean) => void = () => {}
    askConfirm.mockImplementation((message = '', options = {}) => {
      // The real module parks the outstanding request here for the duration.
      pendingConfirm.value = {
        message,
        title: options.title ?? 'Are you sure?',
        destructive: true,
        resolve: () => {},
      }
      return new Promise<boolean>((resolve) => {
        release = (ok: boolean) => {
          pendingConfirm.value = null
          resolve(ok)
        }
      })
    })
    await wrapper.get('.task-delete').trigger('click')
    await flushPromises()
    await nextTick()

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flushPromises()
    await nextTick()

    // Escape belongs to the confirm that is up. Closing the editor behind it
    // would drop the task the confirmation is about.
    expect(wrapper.find('.task-sheet').exists()).toBe(true)
    expect(apiDel).not.toHaveBeenCalled()

    release(true)
    await flushPromises()
    await nextTick()
    expect(apiDel).toHaveBeenCalledWith('/api/tasks/ship', {
      workspace: 'personal',
      expected_revision: NEXT_REVISION,
    })
    wrapper.unmount()
  })

  it('refreshes on mount, so another writer\'s task is there when the pane opens', async () => {
    // The board was left loaded by a previous visit to this workspace; the
    // pane still has to re-read, because `ciao task` or an agent may have
    // added to it since.
    const { useTaskBoardStore } = await import('../../stores/taskBoard')
    const store = useTaskBoardStore()
    store.rows = BOARD
    store.loadedWorkspace = 'personal'

    const extra = task({ id: 'later', title: 'Filed while away' })
    apiGet.mockResolvedValue({ workspace: 'personal', tasks: [...BOARD, extra] })
    const wrapper = mount(TaskBoardView, { attachTo: document.body })
    await flushPromises()
    await nextTick()

    expect(apiGet).toHaveBeenCalledTimes(1)
    expect(wrapper.findAll('.task-card')).toHaveLength(5)
    expect(wrapper.text()).toContain('Filed while away')
    wrapper.unmount()
  })

  it('drops the old workspace on a switch, and closes both dialogs', async () => {
    const store = useProjectStore()
    store.projects = [
      { project_id: 'p1', name: 'Website', workspace: 'personal', context: '', created_at: '2026-03-01', order: 0, vault_folder: '', is_auto: false },
      { project_id: 'p1', name: 'Website', workspace: 'work', context: '', created_at: '2026-03-01', order: 0, vault_folder: '', is_auto: false },
    ] as unknown as typeof store.projects
    apiGet.mockImplementation((url: string) => Promise.resolve(
      url.includes('workspace=work')
        ? { workspace: 'work', tasks: [task({ id: 'other', title: 'Another workspace\'s task' })] }
        : { workspace: 'personal', tasks: BOARD },
    ))
    const wrapper = mount(TaskBoardView, { attachTo: document.body })
    await flushPromises()
    await nextTick()

    await card(wrapper, 'Ship the board').get('.task-open').trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.find('.task-sheet').exists()).toBe(true)

    store.activeWorkspace = 'work'
    await flushPromises()
    await nextTick()

    // The previous workspace's task ids and revisions are gone, so a write from
    // a stale row cannot present them to the new workspace.
    expect(wrapper.text()).not.toContain('Ship the board')
    expect(wrapper.text()).toContain("Another workspace's task")
    expect(wrapper.get('.task-lede').text()).toContain('in work')
    // `1`–`9` can switch workspace with a dialog open, so an open editor would
    // otherwise be editing a task from the workspace the user just left.
    expect(wrapper.find('.task-sheet').exists()).toBe(false)
    wrapper.unmount()
  })

  it('drops a description read that answers after the workspace has changed', async () => {
    const store = useProjectStore()
    let releaseRead: (answer: unknown) => void = () => {}
    apiGet.mockImplementation((url: string) => {
      if (url.includes('workspace=work')) return Promise.resolve({ workspace: 'work', tasks: [] })
      if (url.includes('/api/tasks?')) return Promise.resolve({ workspace: 'personal', tasks: BOARD })
      // The by-id read is still in flight when the user switches with `1`–`9`.
      return new Promise((resolve) => { releaseRead = resolve })
    })
    const wrapper = mount(TaskBoardView, { attachTo: document.body })
    await flushPromises()
    await nextTick()
    await card(wrapper, 'Ship the board').get('.task-open').trigger('click')
    await nextTick()

    store.activeWorkspace = 'work'
    await flushPromises()
    await nextTick()

    // The GET answers now, for the workspace the pane has left.
    releaseRead(detailAnswer(task(), 'Prose from another workspace'))
    await flushPromises()
    await nextTick()

    // A task from the workspace on screen a moment ago, drawn under the new one
    // with an id and a revision the next write from that card would present.
    expect(wrapper.findAll('.task-card')).toHaveLength(0)
    expect(wrapper.text()).not.toContain('Ship the board')
    expect(wrapper.text()).not.toContain('Prose from another workspace')
    wrapper.unmount()
  })

  it('does not let one task\'s late answer land on the dialog opened after it', async () => {
    const wrapper = await mountBoard()
    const pending: Array<(answer: unknown) => void> = []
    apiGet.mockImplementation((url: string) => (url.includes('/api/tasks?')
      ? Promise.resolve({ workspace: 'personal', tasks: BOARD })
      : new Promise((resolve) => { pending.push(resolve) })))

    // Open one task, close it, open another: two reads in flight for two
    // different tasks, either of which can answer first.
    await card(wrapper, 'Ship the board').get('.task-open').trigger('click')
    await nextTick()
    await wrapper.get('.task-sheet-head .btn-icon').trigger('click')
    await nextTick()
    await card(wrapper, 'Wire the store').get('.task-open').trigger('click')
    await nextTick()

    pending[0]!(detailAnswer(task(), 'Prose belonging to Ship the board'))
    await flushPromises()
    await nextTick()

    // The first read is behind the second. It is not this dialog's answer, and it
    // does not get to report a failure either: the read the user is waiting for
    // has not failed, and the field is still the one it was waiting on.
    expect(wrapper.get<HTMLInputElement>('#task-detail-name').element.value).toBe('Wire the store')
    expect(wrapper.get('.task-sheet').text()).toContain('Loading description…')
    expect(wrapper.find('#task-detail-body').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('could not read')
    expect(wrapper.text()).not.toContain('Prose belonging to Ship the board')

    pending[1]!(detailAnswer(task({ id: 'doing', title: 'Wire the store' }), 'Prose belonging to Wire the store'))
    await flushPromises()
    await nextTick()
    expect(wrapper.get<HTMLTextAreaElement>('#task-detail-body').element.value)
      .toBe('Prose belonging to Wire the store')
    wrapper.unmount()
  })

  it('shows the failed state, not the old rows, when the new workspace\'s load fails', async () => {
    const store = useProjectStore()
    apiGet.mockImplementation((url: string) => Promise.resolve(
      url.includes('workspace=work')
        ? Promise.reject(new Error('work vault is offline'))
        : { workspace: 'personal', tasks: BOARD },
    ))
    const wrapper = mount(TaskBoardView, { attachTo: document.body })
    await flushPromises()
    await nextTick()
    expect(wrapper.findAll('.task-card')).toHaveLength(4)

    store.activeWorkspace = 'work'
    await flushPromises()
    await nextTick()

    // Showing the previous workspace's tasks as "as they were last read" for the
    // new one is the worst of the two readings: a move or a Save from those rows
    // would send their ids and revisions to a workspace that has never heard of
    // them.
    expect(wrapper.get('.task-failed-text').text()).toBe('work vault is offline')
    expect(wrapper.findAll('.task-card')).toHaveLength(0)
    expect(wrapper.text()).not.toContain('Ship the board')
    wrapper.unmount()
  })

  it('offers a Reload beside a refused write, and recovers from the 409', async () => {
    const wrapper = await mountBoard()
    apiPatch.mockRejectedValue(Object.assign(new Error('HTTP 409'), {
      payload: { error: { code: 'task_revision_conflict', message: 'that task changed on disk', retryable: true } },
    }))
    await card(wrapper, 'Ship the board').get('select.task-status').setValue('in_progress')
    await flushPromises()
    await nextTick()

    const errorLine = wrapper.get('.task-action-error')
    expect(errorLine.text()).toContain('that task changed on disk')
    const reload = errorLine.findAll('button').find((b) => b.text() === 'Reload')!
    expect(reload.exists()).toBe(true)

    apiPatch.mockResolvedValue({
      workspace: 'personal',
      task: { ...task({ status: 'in_progress' }), revision: NEXT_REVISION, body: '' },
    })
    await reload.trigger('click')
    await flushPromises()
    await nextTick()

    // The reload is what clears the conflict: the board re-reads, and the rows
    // on screen are then the server's again rather than this board's belief.
    expect(wrapper.find('.task-action-error').exists()).toBe(false)
    const [url] = apiGet.mock.calls[apiGet.mock.calls.length - 1]! as [string]
    expect(url).toBe('/api/tasks?workspace=personal')
    // The refused write never reached the file, so the card is back in the
    // column the server still has it in — not the one the select claimed.
    expect(lanes(wrapper)[0]!.text()).toContain('Ship the board')
    expect((card(wrapper, 'Ship the board').get('select.task-status').element as HTMLSelectElement).value)
      .toBe('backlog')
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

  // The pane's own width decides, at the same 940px the CSS breakpoint uses.
  // A window threshold could not: with the sidebar open a wide window can still
  // leave the pane under the breakpoint, and the board then drew four stacked
  // lanes — neither the four columns nor the status-filtered list.
  it('the pane width, not the window, decides between columns and one list', async () => {
    const original = window.innerWidth
    Object.defineProperty(window, 'innerWidth', { value: 1600, configurable: true })
    try {
      const wrapper = await mountBoard()
      reportPaneWidth(1200)
      await nextTick()
      expect(lanes(wrapper)).toHaveLength(4)

      // Narrow pane, wide window: the list, which is the whole point of using
      // the container's width.
      reportPaneWidth(700)
      await nextTick()
      expect(lanes(wrapper)).toHaveLength(1)
      expect(lanes(wrapper)[0]!.get('.task-lane-label').text()).toBe('All tasks')
      expect(wrapper.findAll('.task-card')).toHaveLength(4)
      // …and picking a status is how the list narrows.
      await wrapper.findAll('.task-chip').find((c) => c.text().startsWith('Done'))!.trigger('click')
      await nextTick()
      expect(lanes(wrapper)[0]!.get('.task-lane-label').text()).toBe('Done')
      expect(wrapper.findAll('.task-card')).toHaveLength(1)

      // A status picked is the list on either width, so clearing it first: this
      // is about the width, not the filter.
      await wrapper.findAll('.task-chip').find((c) => c.text().startsWith('All '))!.trigger('click')
      reportPaneWidth(1000)
      await nextTick()
      expect(lanes(wrapper)).toHaveLength(4)
      expect(lanes(wrapper).map((lane) => lane.get('.task-lane-label').text()))
        .toEqual(['Backlog', 'In progress', 'On hold', 'Done'])
      wrapper.unmount()
    } finally {
      Object.defineProperty(window, 'innerWidth', { value: original, configurable: true })
    }
  })

  // ── Delegation (#1033) ──────────────────────────────────────────────────
  //
  // What only a mount can decide here: the badge is drawn for each attempt state,
  // the preview names what the turn will run with, the confirm presents the
  // revision the card was drawn at, the gestures go to the attempt routes, and a
  // workspace switch takes the preview's answer with it. The rest — no
  // `unattended`, one live attempt, the prompt being server-owned — is
  // `tests/test_task_delegation.py` and cannot be decided from a DOM at all.

  const LIVE_ID = 'a'.repeat(32)
  const SETTLED_ID = 'd'.repeat(32)

  /** A card with a live attempt: in progress, for the agent, linked to a chat. */
  function liveTask(overrides: Partial<Task> = {}): Task {
    return task({
      id: 'doing',
      title: 'Wire the store',
      status: 'in_progress',
      assignee: 'agent',
      chat_id: 'chat-7',
      attempt_id: LIVE_ID,
      attempt_state: 'running',
      live_attempt_id: LIVE_ID,
      revision: NEXT_REVISION,
      ...overrides,
    })
  }

  /**
   * A card whose attempt settled and is still linked: the only resumable case, and
   * the one `live_attempt_id` deliberately does not name.
   */
  function settledTask(overrides: Partial<Task> = {}): Task {
    return task({
      id: 'broke',
      title: 'Broke halfway',
      status: 'in_progress',
      assignee: 'agent',
      chat_id: 'chat-7',
      attempt_id: SETTLED_ID,
      attempt_state: 'interrupted',
      live_attempt_id: '',
      revision: NEXT_REVISION,
      ...overrides,
    })
  }

  /** A card with nothing delegated: what a first delegation is offered from. */
  function plainTask(overrides: Partial<Task> = {}): Task {
    return task({ id: 'fresh', title: 'Draft the runbook', ...overrides })
  }

  /** The `POST .../delegate` answer, as the service writes it. */
  function delegateAnswer(fields: Record<string, unknown> = {}) {
    const attempt = {
      attempt_id: 'b'.repeat(32),
      task_id: 'fresh',
      task_revision: REVISION,
      chat_id: 'chat-9',
      state: 'running',
      created_at: '2026-03-02T09:00:00+00:00',
      updated_at: '2026-03-02T09:00:00+00:00',
      ended_at: '',
      detail: '',
      live: true,
      ...fields,
    }
    return {
      workspace: 'personal',
      created: true,
      attempt,
      chat_id: attempt.chat_id,
      project_id: 'p1',
      project_origin: 'task',
      changed_since_delegated: false,
      task: {
        ...plainTask(),
        status: 'in_progress',
        assignee: 'agent',
        chat_id: 'chat-9',
        attempt_id: attempt.attempt_id,
        revision: THIRD_REVISION,
        attempt_state: 'running',
        live_attempt_id: attempt.attempt_id,
        body: '',
      },
    }
  }

  /**
   * Mount a board whose rows answer by id with a body.
   *
   * The description read is wired here rather than after the mount, because
   * `openDelegate` reads it as soon as the card is clicked: a mock set afterwards
   * would answer the wrong question. The read answers at `bodyRevision`, which
   * defaults to the row's own — the case where the board and the sheet agree.
   */
  async function mountWithBody(
    rows: Task[],
    body = 'Wire the store.',
    bodyRevision?: string,
  ) {
    apiGet.mockImplementation((url: string) => Promise.resolve(
      url.includes('/api/tasks?')
        ? { workspace: 'personal', tasks: rows }
        : { workspace: 'personal', task: { ...rows[0], body, revision: bodyRevision ?? rows[0]!.revision } },
    ))
    const wrapper = mount(TaskBoardView, { attachTo: document.body })
    await flushPromises()
    await nextTick()
    return wrapper
  }

  /** The card chip with this exact label. */
  function chip(wrapper: ReturnType<typeof mount>, title: string, label: string) {
    return card(wrapper, title).findAll('.task-chip').find((c) => c.text() === label)!
  }

  /**
   * The delegation preview sheet.
   *
   * Not just the first `.task-sheet`: the detail dialog is one too, and both can be
   * open at once — the preview is opened *from* the editor. Found by its own
   * heading, which is what distinguishes them. Returns `undefined` once closed, so
   * a test can ask whether it is still there.
   */
  function delegateSheet(wrapper: ReturnType<typeof mount>) {
    return wrapper.findAll('.task-sheet').find((s) => s.find('#task-delegate-title').exists())
  }

  /** Open the delegation preview on a card. */
  async function openPreview(wrapper: ReturnType<typeof mount>, title: string) {
    await chip(wrapper, title, 'Delegate').trigger('click')
    await flushPromises()
    await nextTick()
  }

  it('draws the attempt badge for every state, coloured by what it means',
    async () => {
      const wrapper = await mountWithBody([
        task({ id: 'a', title: 'Never delegated' }),
        liveTask(),
        task({
          id: 'b', title: 'Waiting on you', status: 'on_hold',
          attempt_state: 'needs_you', live_attempt_id: 'c'.repeat(32),
        }),
        task({
          id: 'c', title: 'Finished', status: 'done',
          attempt_state: 'ready_for_review', live_attempt_id: 'd'.repeat(32),
          review_state: 'ready',
        }),
        task({ id: 'd', title: 'Dead turn', attempt_state: 'failed' }),
      ])

      const badge = (title: string) => card(wrapper, title).get('.badge')
      // In flight, waiting on the user, ready to review, and over — four states,
      // four classes, because a paused turn and a failed one are different facts.
      expect(badge('Wire the store').text()).toBe('Running')
      expect(badge('Wire the store').classes()).toContain('badge--muted')
      expect(badge('Waiting on you').text()).toBe('Needs you')
      expect(badge('Waiting on you').classes()).toContain('badge--accent2')
      expect(badge('Dead turn').text()).toBe('Failed')
      expect(badge('Dead turn').classes()).toContain('badge--error')
      // And a finished turn is a badge beside Review, never a Done the board moved
      // on its own.
      expect(badge('Finished').text()).toBe('Review ready')
      expect(card(wrapper, 'Finished').findAll('.badge')).toHaveLength(2)
      // A task nobody delegated has no badge at all.
      expect(card(wrapper, 'Never delegated').findAll('.badge')).toHaveLength(0)
      wrapper.unmount()
    })

  it('says the result was reached against an older description', async () => {
    const wrapper = await mountWithBody([
      liveTask(),
      task({
        id: 'b', title: 'Waiting on you', status: 'on_hold',
        changed_since_delegated: true, attempt_state: 'ready_for_review',
        live_attempt_id: 'c'.repeat(32),
      }),
    ])

    expect(card(wrapper, 'Waiting on you').get('.task-changed').text())
      .toContain('Changed since delegated')
    expect(card(wrapper, 'Wire the store').find('.task-changed').exists()).toBe(false)
    wrapper.unmount()
  })

  it('previews the resolved project, the defaults, and that the turn is attended',
    async () => {
      const wrapper = await mountWithBody([plainTask({ project_id: 'p1' })], 'Write the runbook.')

      await openPreview(wrapper, 'Draft the runbook')

      const sheet = wrapper.get('.task-sheet')
      // The task's own project, not a guess: `project_id: 'p1'` on the card.
      expect(sheet.get('.task-preview-facts').text()).toContain('Website')
      // The model and provider are the operator's own defaults, said as such
      // rather than shown as a value the board cannot promise.
      expect(sheet.get('.task-preview-facts').text()).toContain('workspace default')
      // And the one fact a board cannot imply.
      expect(sheet.text()).toContain('Attended')
      // The description that goes into the chat, read by id because a row carries
      // none — a task with no prose is still delegated, and says so rather than
      // showing an empty box the user might confirm.
      expect(sheet.text()).toContain('Write the runbook.')
      wrapper.unmount()
    })

  it('confirms the delegation at the revision the card was drawn at', async () => {
    apiPost.mockResolvedValue(delegateAnswer())
    const wrapper = await mountWithBody([plainTask({ project_id: 'p1' })])

    await openPreview(wrapper, 'Draft the runbook')
    await wrapper.get('.task-sheet .btn-primary').trigger('click')
    await flushPromises()

    expect(apiPost).toHaveBeenCalledWith(
      '/api/tasks/fresh/delegate',
      { workspace: 'personal', expected_revision: REVISION },
    )
    // The card now carries the attempt the answer named: it is the agent's, linked
    // to the chat the turn runs in, and badged as running.
    const delegated = card(wrapper, 'Draft the runbook')
    expect(delegated.get('.badge').text()).toBe('Running')
    expect(delegated.text()).toContain('For the agent')
    // And the gesture set replaced the single Delegate control.
    const labels = delegated.findAll('.task-chip').map((c) => c.text())
    expect(labels).toContain('Stop')
    expect(labels).toContain('Detach')
    expect(labels).not.toContain('Delegate')
    wrapper.unmount()
  })

  it('sends a named project only when one was chosen', async () => {
    apiPost.mockResolvedValue(delegateAnswer())
    const wrapper = await mountWithBody([plainTask()])

    // The card names no project, so the preview says General and the request
    // carries no override — the server's own resolution, not a client default.
    await openPreview(wrapper, 'Draft the runbook')
    expect(wrapper.get('.task-preview-facts').text()).toContain('General')
    await wrapper.get('.task-sheet .btn-primary').trigger('click')
    await flushPromises()

    expect(apiPost).toHaveBeenCalledWith(
      '/api/tasks/fresh/delegate',
      { workspace: 'personal', expected_revision: REVISION },
    )
    wrapper.unmount()
  })

  it('offers the gestures instead of a second delegation while an attempt is live',
    async () => {
      const wrapper = await mountWithBody([task({ id: 'plain', title: 'Never delegated' }), liveTask()])

      const labels = (title: string) =>
        card(wrapper, title).findAll('.task-chip').map((c) => c.text())
      expect(labels('Wire the store')).toEqual(['Chat', 'Stop', 'Detach', 'Done'])
      // A task with nothing delegated offers the one gesture that starts work, and
      // no Chat control: the board only links a chat it was told about.
      expect(labels('Never delegated')).toEqual(['Delegate', 'Done'])
      wrapper.unmount()
    })

  it('offers Resume and Retry separately for a settled attempt still linked',
    async () => {
      const wrapper = await mountWithBody([settledTask()])

      // The only case the server will resume, and it had no control at all before:
      // a plain "Delegate" here starts a *second* attempt in a new chat and
      // abandons the one that failed. Resume and Retry are the two real answers,
      // so they are two labelled controls rather than one that has to guess.
      expect(card(wrapper, 'Broke halfway').findAll('.task-chip').map((c) => c.text()))
        .toEqual(['Chat', 'Resume', 'Retry', 'Done'])

      await chip(wrapper, 'Broke halfway', 'Resume').trigger('click')
      await flushPromises()
      expect(apiPost).toHaveBeenCalledWith(
        `/api/tasks/broke/attempt/${SETTLED_ID}/resume`,
        { workspace: 'personal' },
      )

      await chip(wrapper, 'Broke halfway', 'Retry').trigger('click')
      await flushPromises()
      expect(apiPost).toHaveBeenCalledWith(
        `/api/tasks/broke/attempt/${SETTLED_ID}/retry`,
        { workspace: 'personal' },
      )
      wrapper.unmount()
    })

  it('offers a live attempt its chat to read, never a resume', async () => {
    // Every live state — `running`, `needs_you`, `ready_for_review` — is refused by
    // the server's `resume`, so "Continue this chat" was a control that always
    // errored. What a live attempt needs is to be read where it is running.
    for (const state of ['running', 'needs_you', 'ready_for_review'] as const) {
      const wrapper = await mountWithBody([liveTask({ attempt_state: state })])
      await card(wrapper, 'Wire the store').get('.task-open').trigger('click')
      await flushPromises()
      await nextTick()

      const controls = wrapper.findAll('.task-delegate-actions .btn-chip').map((c) => c.text())
      expect(controls[0], state).toBe('Open chat')
      expect(controls, state).not.toContain('Resume')
      expect(controls, state).not.toContain('Retry')
      expect(wrapper.findAll('.btn-chip').map((c) => c.text()), state)
        .not.toContain('Continue this chat')
      wrapper.unmount()
    }
  })

  it('opens the live attempt chat from the editor rather than resuming it',
    async () => {
      const wrapper = await mountWithBody([liveTask()])
      const switchChat = vi.fn()
      useProjectStore().switchChat = switchChat

      await card(wrapper, 'Wire the store').get('.task-open').trigger('click')
      await flushPromises()
      await nextTick()
      await wrapper.findAll('.btn-chip').find((c) => c.text() === 'Open chat')!
        .trigger('click')
      await flushPromises()
      await nextTick()

      // The preview explains why there is nothing to confirm, and nothing was sent.
      expect(delegateSheet(wrapper)!.text()).toContain('still live')
      expect(apiPost).not.toHaveBeenCalled()
      await delegateSheet(wrapper)!.get('.btn-primary').trigger('click')
      await flushPromises()
      expect(switchChat).toHaveBeenCalledWith('chat-7')
      // And it closed rather than sitting there with a spent confirmation.
      expect(delegateSheet(wrapper)).toBeUndefined()
      wrapper.unmount()
    })

  it('names both ways on a settled attempt in the preview', async () => {
    apiPost.mockResolvedValue({
      workspace: 'personal',
      attempt: { ...delegateAnswer().attempt, attempt_id: SETTLED_ID, chat_id: 'chat-7', state: 'running' },
      chat_id: 'chat-7',
      resumed: true,
    })
    const wrapper = await mountWithBody([settledTask()])

    await card(wrapper, 'Broke halfway').get('.task-open').trigger('click')
    await flushPromises()
    await nextTick()
    // The editor's control opens the preview in the mode the row calls for — the
    // one place that names what will happen, and the one place that does it.
    const control = wrapper.findAll('.task-delegate-actions .btn-chip')
      .find((c) => c.text() === 'Resume')
    expect(control, 'no Resume control for a settled attempt').toBeTruthy()
    await control!.trigger('click')
    await flushPromises()
    await nextTick()

    const sheet = delegateSheet(wrapper)!
    // The confirm continues that attempt's own chat under its own id — no revision,
    // because the gesture acts on an attempt rather than editing a record.
    expect(sheet.get('.btn-primary').text()).toBe('Resume')
    expect(sheet.text()).toContain('Resuming continues that attempt in its own chat')
    // And the other way out is named in the same place, so neither has to be found.
    expect(sheet.findAll('.btn-small').map((b) => b.text()))
      .toEqual(['Start a new attempt', 'Cancel'])
    await sheet.get('.btn-primary').trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith(
      `/api/tasks/broke/attempt/${SETTLED_ID}/resume`,
      { workspace: 'personal' },
    )
    wrapper.unmount()
  })

  it('starts a new attempt from the preview as a separate labelled gesture',
    async () => {
      apiPost.mockResolvedValue(delegateAnswer())
      const wrapper = await mountWithBody([settledTask()])

      await card(wrapper, 'Broke halfway').get('.task-open').trigger('click')
      await flushPromises()
      await nextTick()
      await wrapper.findAll('.task-delegate-actions .btn-chip')
        .find((c) => c.text() === 'Resume')!.trigger('click')
      await flushPromises()
      await nextTick()
      await wrapper.findAll('.btn-small').find((b) => b.text() === 'Start a new attempt')!
        .trigger('click')
      await flushPromises()
      await nextTick()

      const sheet = delegateSheet(wrapper)!
      expect(sheet.get('.btn-primary').text()).toBe('Start a new attempt')
      expect(sheet.text()).toContain('The previous attempt stays as history')
      await sheet.get('.btn-primary').trigger('click')
      await flushPromises()
      // A fresh delegation: a new attempt, not the old one continued.
      expect(apiPost).toHaveBeenCalledWith(
        '/api/tasks/broke/delegate',
        { workspace: 'personal', expected_revision: NEXT_REVISION },
      )
      wrapper.unmount()
    })

  it('confirms at the revision the previewed description was read at', async () => {
    // A reload, an adopted write, or another tab can put a newer revision on the row
    // while the sheet is open. The user has read the body at the *old* one, so the
    // confirm presents that — a body that moved on is a 409 the preview re-reads,
    // not a turn launched from a description nobody saw.
    apiPost.mockResolvedValue(delegateAnswer())
    const wrapper = await mountWithBody([plainTask({ project_id: 'p1' })], 'The scope.', REVISION)

    await openPreview(wrapper, 'Draft the runbook')
    expect(wrapper.get('.task-sheet').text()).toContain('The scope.')
    // The row moves on under the open sheet.
    const store = useTaskBoardStore()
    store.$patch({ rows: [plainTask({ project_id: 'p1', revision: THIRD_REVISION })] })

    await wrapper.get('.task-sheet .btn-primary').trigger('click')
    await flushPromises()

    expect(apiPost).toHaveBeenCalledWith(
      '/api/tasks/fresh/delegate',
      { workspace: 'personal', expected_revision: REVISION },
    )
    wrapper.unmount()
  })

  it('stops and detaches through the attempt routes', async () => {
    apiPost.mockResolvedValue({
      workspace: 'personal',
      attempt: { ...delegateAnswer().attempt, attempt_id: LIVE_ID, chat_id: 'chat-7', state: 'stopped', live: false },
      chat_id: 'chat-7',
    })
    const wrapper = await mountWithBody([liveTask()])

    await chip(wrapper, 'Wire the store', 'Stop').trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith(
      `/api/tasks/doing/attempt/${LIVE_ID}/stop`,
      { workspace: 'personal' },
    )
    wrapper.unmount()

    // Detach is a gesture on a *live* attempt, so it is read on a card that still
    // has one — a stopped attempt has nothing running to stop before releasing.
    const releasing = await mountWithBody([liveTask()])
    await chip(releasing, 'Wire the store', 'Detach').trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith(
      `/api/tasks/doing/attempt/${LIVE_ID}/detach`,
      { workspace: 'personal' },
    )
    releasing.unmount()
  })

  it('updates the card from the stop reply rather than leaving it running',
    async () => {
      // A Stop leaves the linkage in place but still moves the record, and the store
      // adopts the row from the reply's `task`. A reply without it would leave the
      // card reading "Running" over a turn the user has just ended — which is
      // exactly what `actOnAttempt` used to leave behind, since it only reloaded
      // on an error.
      apiPost.mockResolvedValue({
        workspace: 'personal',
        attempt: { ...delegateAnswer().attempt, attempt_id: LIVE_ID, chat_id: 'chat-7', state: 'stopped', live: false },
        chat_id: 'chat-7',
        task: {
          ...liveTask(),
          attempt_state: 'stopped',
          live_attempt_id: '',
          revision: THIRD_REVISION,
        },
      })
      const wrapper = await mountWithBody([liveTask()])

      await chip(wrapper, 'Wire the store', 'Stop').trigger('click')
      await flushPromises()

      expect(card(wrapper, 'Wire the store').get('.badge').text()).toBe('Stopped')
      expect(card(wrapper, 'Wire the store').findAll('.task-chip').map((c) => c.text()))
        .toEqual(['Chat', 'Resume', 'Retry', 'Done'])
      wrapper.unmount()
    })

  it('closes a review-ready card with Done, in one gesture', async () => {
    // The review is the case the issue describes, and Done is the button for it.
    // The service releases the linkage and completes together, so the card needs
    // no Detach first — and the attempt stays as review-ready history.
    const reviewed = liveTask({
      attempt_state: 'ready_for_review', review_state: 'ready', revision: THIRD_REVISION,
    })
    apiPost.mockResolvedValue({
      workspace: 'personal',
      task: {
        ...reviewed,
        status: 'done',
        chat_id: '',
        attempt_id: '',
        review_state: 'none',
        attempt_state: 'ready_for_review',
        live_attempt_id: '',
        revision: NEXT_REVISION,
      },
    })
    const wrapper = await mountWithBody([reviewed])

    await chip(wrapper, 'Wire the store', 'Done').trigger('click')
    await flushPromises()

    // One request, the completion route — no attempt gesture in between.
    expect(apiPost).toHaveBeenCalledTimes(1)
    expect(apiPost).toHaveBeenCalledWith('/api/tasks/doing/complete', {
      workspace: 'personal',
      expected_revision: THIRD_REVISION,
    })
    // And the card moved, which is the whole point of the gesture.
    expect(
      wrapper.findAll('.task-lane').find((l) => l.text().includes('Done'))!
        .findAll('.task-card').map((c) => c.text()),
    ).toEqual([expect.stringContaining('Wire the store')])
    wrapper.unmount()
  })

  it('opens the attempt chat through the project store, not a route', async () => {
    const wrapper = await mountWithBody([liveTask()])
    const switchChat = vi.fn()
    // The sidebar row and the in-app toast both go through this; a delegation
    // specific route would be a second path to a chat and would drift.
    useProjectStore().switchChat = switchChat

    await chip(wrapper, 'Wire the store', 'Chat').trigger('click')

    expect(switchChat).toHaveBeenCalledWith('chat-7')
    wrapper.unmount()
  })

  it('drops a delegation whose answer arrives after the workspace changed',
    async () => {
      const store = useProjectStore()
      let releaseRead: (answer: unknown) => void = () => {}
      let releasePost: (answer: unknown) => void = () => {}
      apiGet.mockImplementation((url: string) => {
        if (url.includes('workspace=work')) {
          return Promise.resolve({ workspace: 'work', tasks: [] })
        }
        if (url.includes('/api/tasks?')) {
          return Promise.resolve({ workspace: 'personal', tasks: [plainTask()] })
        }
        return new Promise((resolve) => { releaseRead = resolve })
      })
      apiPost.mockReturnValue(new Promise((resolve) => { releasePost = resolve }))
      const wrapper = mount(TaskBoardView, { attachTo: document.body })
      await flushPromises()
      await nextTick()

      await chip(wrapper, 'Draft the runbook', 'Delegate').trigger('click')
      await nextTick()

      store.activeWorkspace = 'work'
      await flushPromises()
      await nextTick()
      // The preview closed with the switch: confirming it would have launched a
      // turn in the workspace the user left, described by a task the new one never
      // saw.
      expect(wrapper.find('.task-sheet').exists()).toBe(false)

      // Both answers land now, for the workspace the pane has left.
      releaseRead({ workspace: 'personal', task: { ...plainTask(), body: 'Old prose' } })
      releasePost(delegateAnswer())
      await flushPromises()
      await nextTick()

      expect(wrapper.text()).not.toContain('Draft the runbook')
      expect(wrapper.text()).not.toContain('Old prose')
      expect(wrapper.findAll('.task-card')).toHaveLength(0)
      wrapper.unmount()
    })

  it('keeps a refusal in the action slot with the rows it had', async () => {
    apiPost.mockRejectedValue(Object.assign(new Error('HTTP 409'), {
      payload: {
        error: {
          code: 'task_revision_conflict',
          message: 'The task changed since this delegation was planned; nothing was started.',
          retryable: true,
        },
      },
    }))
    const wrapper = await mountWithBody([plainTask()])

    await openPreview(wrapper, 'Draft the runbook')
    await wrapper.get('.task-sheet .btn-primary').trigger('click')
    await flushPromises()

    // The server's own sentence, and the rows it had: a 409 is the record moving
    // on, which re-reading fixes — so the preview stays open with what was typed
    // and the card is still a card.
    expect(wrapper.get('.task-action-error').text())
      .toContain('The task changed since this delegation was planned')
    expect(card(wrapper, 'Draft the runbook').exists()).toBe(true)
    wrapper.unmount()
  })

  it('says the description could not be read rather than confirming blind',
    async () => {
      const wrapper = await mountWithBody([plainTask()], 'Wire the store.')
      apiGet.mockImplementation((url: string) => {
        if (url.includes('/api/tasks?')) {
          return Promise.resolve({ workspace: 'personal', tasks: [plainTask()] })
        }
        return Promise.reject(new Error('no description'))
      })
      await chip(wrapper, 'Draft the runbook', 'Delegate').trigger('click')
      await flushPromises()
      await nextTick()

      const sheet = wrapper.get('.task-sheet')
      expect(sheet.text()).toContain('could not read')
      // And a retry is offered rather than the confirm being the only way on.
      expect(sheet.findAll('.btn-chip').map((c) => c.text())).toContain('Retry')
      wrapper.unmount()
    })
})

/** The pane's Retry path: a reload against the same workspace. */
async function useTaskBoardReload(wrapper: ReturnType<typeof mount>) {
  const { useTaskBoardStore } = await import('../../stores/taskBoard')
  await useTaskBoardStore().reload('personal')
  return wrapper
}