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
    chat_id: '',
    attempt_id: '',
    created_at: '2026-03-01T09:00:00+00:00',
    updated_at: '2026-03-01T09:00:00+00:00',
    revision: REVISION,
    relative_path: 'Tasks/ship.md',
    attempt_state: '',
    attempt_outcome: '',
    attempt_summary: '',
    attempt_detail: '',
    live_attempt_id: '',
    changed_since_delegated: false,
    ...overrides,
  }
}

const BOARD: TaskRow[] = [
  task(),
  task({ id: 'doing', title: 'Wire the store', status: 'in_progress', project_id: 'p1' }),
  task({ id: 'held', title: 'Wait on a key', status: 'in_review' }),
  task({ id: 'done', title: 'Land the types', status: 'done', assignee: 'agent' }),
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

/**
 * The description's editor. A described task opens rendered, so this presses
 * Edit first when the textarea is not already up.
 */
async function bodyField(wrapper: ReturnType<typeof mount>) {
  if (!wrapper.find('#task-detail-body').exists()) {
    await wrapper.findAll('.task-field-head .task-chip').find((c) => c.text() === 'Edit')!.trigger('click')
    await nextTick()
  }
  return wrapper.get<HTMLTextAreaElement>('#task-detail-body')
}

/** Drag a card onto one of the four columns, as the browser's events arrive. */
async function dragCard(wrapper: ReturnType<typeof mount>, title: string, laneIndex: number) {
  const lane = lanes(wrapper)[laneIndex]!
  await card(wrapper, title).trigger('dragstart')
  await lane.trigger('dragover')
  await lane.trigger('drop')
  await card(wrapper, title).trigger('dragend')
  await flushPromises()
  await nextTick()
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
      { project_id: 'general', name: 'General', workspace: 'personal', context: '', created_at: '2026-03-01', order: 1, vault_folder: '', is_auto: true },
      { project_id: 'p2', name: 'Docs', workspace: 'personal', context: '', created_at: '2026-03-01', order: 2, vault_folder: '', is_auto: false },
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
      .toEqual(['To do', 'In progress', 'In review', 'Done'])
    // The count is the board's own, read off the rows rather than recomputed
    // from the filters, so a filtered column still says what is in it.
    expect(lanes(wrapper).map((lane) => lane.get('.badge').text())).toEqual(['1', '1', '1', '1'])
    expect(wrapper.get('.task-lede').text()).toContain('3 open of 4')
    wrapper.unmount()
  })

  it('shows a card\'s title, project and due date, and no assignee or review badge', async () => {
    const wrapper = await mountBoard([
      task({
        id: 'ship',
        title: 'Ship the board',
        project_id: 'p1',
        // Far enough out that the card is not also "overdue", which is a
        // separate assertion in its own right.
        due: '2099-03-20',
        assignee: 'agent',
      }),
    ])

    const shown = card(wrapper, 'Ship the board')
    expect(shown.get('.task-open').text()).toBe('Ship the board')
    expect(shown.get('.task-project').text()).toBe('Website')
    expect(shown.text()).toContain('Due Mar 20')
    // Who it is for is the Agent block's business, not a label on every card.
    expect(shown.text()).not.toContain('For the agent')
    // Review is the column a card sits in (#1069), not a badge on it.
    expect(shown.text()).not.toContain('Review')
    wrapper.unmount()
  })

  it('marks a past due date overdue in words as well as in colour', async () => {
    const wrapper = await mountBoard([task({ due: '2000-01-01' })])
    expect(card(wrapper, 'Ship the board').text()).toContain('Overdue')
    wrapper.unmount()
  })

  it('says nothing for the defaults: General, For me and no date leave no meta line', async () => {
    const wrapper = await mountBoard([task()])
    const shown = card(wrapper, 'Ship the board')
    expect(shown.find('.task-meta').exists()).toBe(false)
    // And an undelegated card has no foot: its Done is the checkbox.
    expect(shown.find('.task-card-foot').exists()).toBe(false)
    expect(shown.find('select').exists()).toBe(false)
    wrapper.unmount()
  })

  it('names the status on a card in the mixed narrow list, where no column does', async () => {
    const wrapper = await mountBoard()
    reportPaneWidth(390)
    await nextTick()
    expect(lanes(wrapper)).toHaveLength(1)
    expect(card(wrapper, 'Wait on a key').get('.task-status-badge').text()).toBe('In review')
    // Nothing to drag between in a list.
    expect(card(wrapper, 'Wait on a key').attributes('draggable')).toBe('false')
    wrapper.unmount()
  })

  it('moves a card at the revision it drew the card from', async () => {
    const wrapper = await mountBoard()
    apiPatch.mockResolvedValue({ workspace: 'personal', task: { ...task({ status: 'in_progress' }), revision: NEXT_REVISION, body: '' } })

    await dragCard(wrapper, 'Ship the board', 1)

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
    wrapper.unmount()
  })

  it('ignores a drop on the column the card is already in', async () => {
    const wrapper = await mountBoard()
    await dragCard(wrapper, 'Ship the board', 0)
    expect(apiPatch).not.toHaveBeenCalled()
    expect(apiPost).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('completes a card dropped on Done through the completion gesture', async () => {
    const wrapper = await mountBoard()
    apiPost.mockResolvedValue({ workspace: 'personal', task: { ...task({ status: 'done' }), revision: NEXT_REVISION, body: '' } })
    await dragCard(wrapper, 'Ship the board', 3)
    expect(apiPost).toHaveBeenCalledWith('/api/tasks/ship/complete', {
      workspace: 'personal',
      expected_revision: REVISION,
    })
    expect(apiPatch).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('moves a focused card one column over with Shift+Arrow, and not with Option+Arrow', async () => {
    const wrapper = await mountBoard()
    apiPatch.mockResolvedValue({ workspace: 'personal', task: { ...task({ status: 'in_progress' }), revision: NEXT_REVISION, body: '' } })
    const title = card(wrapper, 'Ship the board').get('.task-open')
    // Option+Arrow is the app's section switch; the card leaves it alone.
    await title.trigger('keydown', { key: 'ArrowRight', altKey: true })
    expect(apiPatch).not.toHaveBeenCalled()
    // Leftmost already: nothing to the left.
    await title.trigger('keydown', { key: 'ArrowLeft', shiftKey: true })
    expect(apiPatch).not.toHaveBeenCalled()
    await title.trigger('keydown', { key: 'ArrowRight', shiftKey: true })
    await flushPromises()
    await nextTick()
    expect(apiPatch).toHaveBeenCalledTimes(1)
    expect((apiPatch.mock.calls[0]![1] as Record<string, unknown>).status).toBe('in_progress')
    expect(card(wrapper, 'Ship the board').element.closest('.task-lane')!.getAttribute('aria-label')).toBe('In progress')
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

    await dragCard(wrapper, 'Ship the board', 1)

    // The server's own words, not a generic failure and not `[object Object]`:
    // this surface nests its message inside the envelope.
    expect(wrapper.get('.task-action-error').text()).toContain('that task changed on disk')
    // The row is still the card it was, in the column it was in — a refused
    // write must not cost the user the card.
    expect(lanes(wrapper)[0]!.text()).toContain('Ship the board')
    expect(lanes(wrapper)[1]!.text()).not.toContain('Ship the board')
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

    expect(wrapper.get('.skeleton').attributes('aria-label')).toBe('Loading tasks')
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
    // No task on this board is filed under Docs, so the filter hides the
    // whole set while the board itself is not empty.
    await wrapper.get('#task-filter-project').setValue('p2')
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

    // It opens as it reads: rendered, and the raw tag shown as typed rather than
    // parsed. No second copy of it under a textarea.
    expect(wrapper.find('#task-detail-body').exists()).toBe(false)
    expect(wrapper.find('.task-preview').exists()).toBe(false)
    expect(wrapper.get('.task-body').html()).toContain('&lt;b&gt;not html&lt;/b&gt;')
    expect(wrapper.get('.task-body').element.querySelector('b')).toBeNull()

    // Edit swaps the rendered text for the source.
    const textarea = await bodyField(wrapper)
    expect(textarea.element.value).toContain('<b>not html</b>')
    expect(wrapper.find('.task-body').exists()).toBe(false)
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
    expect(wrapper.get('.task-body').text()).toBe('OLD')
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
    const textarea = await bodyField(wrapper)
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
    const textarea = await bodyField(wrapper)
    expect(textarea.element.value).toContain('<b>not html</b>')
    expect(wrapper.get('.task-sheet').text()).not.toContain('could not read')
    wrapper.unmount()
  })

  it('calls one place General: no "No project", no second General', async () => {
    const wrapper = await mountBoard([
      task(),
      task({ id: 'gen', title: 'Filed under the auto General', project_id: 'general' }),
      task({ id: 'web', title: 'Website work', project_id: 'p1' }),
    ])
    const labels = (selector: string) =>
      wrapper.get(selector).findAll('option').map((o) => o.text().trim())
    expect(labels('#task-filter-project')).toEqual(['All projects', 'General', 'Website', 'Docs'])

    // Both kinds of General answer to the one filter entry.
    await wrapper.get('#task-filter-project').setValue('no-project')
    await nextTick()
    expect(wrapper.findAll('.task-card').map((c) => c.get('.task-open').text()))
      .toEqual(expect.arrayContaining(['Ship the board', 'Filed under the auto General']))
    expect(wrapper.text()).not.toContain('Website work')
    // And neither card spells it out: General is the default.
    expect(card(wrapper, 'Filed under the auto General').find('.task-project').exists()).toBe(false)

    apiGet.mockResolvedValue(detailAnswer(task({ id: 'gen', project_id: 'general' }), ''))
    await card(wrapper, 'Filed under the auto General').get('.task-open').trigger('click')
    await flushPromises()
    await nextTick()
    expect(labels('#task-detail-project')).toEqual(['General', 'Website', 'Docs'])
    // Opening it shows General selected and writes nothing.
    expect((wrapper.get('#task-detail-project').element as HTMLSelectElement).value).toBe('')
    expect(apiPatch).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('edits the description without the engine\'s log, and keeps the log on save', async () => {
    const log = '<!-- ciao:task-log -->\n## Delegation log\n\n- a line <!-- attempt:x -->\n<!-- /ciao:task-log -->'
    const wrapper = await mountBoard()
    apiGet.mockResolvedValue(detailAnswer(task(), `Ship it.\n\n${log}\n`))
    apiPatch.mockResolvedValue({ workspace: 'personal', task: { ...task(), revision: THIRD_REVISION, body: '' } })
    await card(wrapper, 'Ship the board').get('.task-open').trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.get('.task-body').text()).toBe('Ship it.')
    const field = await bodyField(wrapper)
    expect(field.element.value).toBe('Ship it.\n')
    await field.setValue('Ship it today.\n')
    await field.trigger('blur')
    await flushPromises()
    expect((apiPatch.mock.calls[0]![1] as Record<string, unknown>).body).toBe(`Ship it today.\n\n${log}\n`)
    wrapper.unmount()
  })

  it('saves the editor as it changes, with no Save button', async () => {
    const wrapper = await mountBoard()
    apiGet.mockResolvedValue(detailAnswer(task(), ''))
    apiPatch.mockImplementation((_path: string, sent: Record<string, unknown>) => Promise.resolve({
      workspace: 'personal',
      task: { ...task(), ...sent, revision: THIRD_REVISION, body: '' },
    }))
    await card(wrapper, 'Ship the board').get('.task-open').trigger('click')
    await flushPromises()
    await nextTick()
    const sheet = wrapper.get('.task-sheet')
    expect(sheet.findAll('button').map((b) => b.text())).not.toContain('Save')
    expect(sheet.findAll('button').map((b) => b.text())).not.toContain('Cancel')

    // A select writes the moment it changes.
    await wrapper.get('#task-detail-project').setValue('p1')
    await flushPromises()
    expect(apiPatch).toHaveBeenCalledTimes(1)
    expect(apiPatch.mock.calls[0]![1]).toMatchObject({ project_id: 'p1', expected_revision: NEXT_REVISION })

    // Typing waits for a pause; leaving the field writes it now, at the revision
    // the previous write returned.
    await wrapper.get('#task-detail-name').setValue('Ship the board today')
    await nextTick()
    expect(apiPatch).toHaveBeenCalledTimes(1)
    expect(wrapper.get('.task-saved').text()).toBe('Saving…')
    await wrapper.get('#task-detail-name').trigger('blur')
    await flushPromises()
    expect(apiPatch).toHaveBeenCalledTimes(2)
    expect(apiPatch.mock.calls[1]![1]).toMatchObject({ title: 'Ship the board today', expected_revision: THIRD_REVISION })
    expect(apiPatch.mock.calls[1]![1]).not.toHaveProperty('project_id')
    await nextTick()
    expect(wrapper.get('.task-saved').text()).toBe('Saved')
    wrapper.unmount()
  })

  it('writes a pending edit on close, and stays open when the write is refused', async () => {
    const wrapper = await mountBoard()
    apiGet.mockResolvedValue(detailAnswer(task(), ''))
    apiPatch.mockRejectedValueOnce(Object.assign(new Error('HTTP 409'), {
      payload: { error: { code: 'task_revision_conflict', message: 'that task changed on disk', retryable: true } },
    }))
    await card(wrapper, 'Ship the board').get('.task-open').trigger('click')
    await flushPromises()
    await nextTick()

    await wrapper.get('#task-detail-name').setValue('Renamed')
    await wrapper.get('.task-sheet-head .btn-icon').trigger('click')
    await flushPromises()
    await nextTick()
    expect(apiPatch).toHaveBeenCalledTimes(1)
    expect(wrapper.find('.task-sheet').exists()).toBe(true)
    expect(wrapper.get('.task-sheet').text()).toContain('that task changed on disk')

    apiPatch.mockResolvedValue({
      workspace: 'personal',
      task: { ...task({ title: 'Renamed' }), revision: THIRD_REVISION, body: '' },
    })
    await wrapper.get('.task-sheet-head .btn-icon').trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.find('.task-sheet').exists()).toBe(false)
    wrapper.unmount()
  })

  it('moves the dialog\'s status without completing the task', async () => {
    const wrapper = await mountBoard()
    apiGet.mockResolvedValue(detailAnswer(task(), ''))
    apiPatch.mockResolvedValue({
      workspace: 'personal',
      task: { ...task({ status: 'in_review' }), revision: NEXT_REVISION, body: '' },
    })
    await card(wrapper, 'Ship the board').get('.task-open').trigger('click')
    await flushPromises()
    await nextTick()

    await wrapper.get('[data-status="in_review"]').trigger('click')
    await flushPromises()
    await nextTick()

    // "In review" is a move, not the completion gesture: the record must not be
    // marked done because the select changed.
    expect(apiPost).not.toHaveBeenCalled()
    expect(apiPatch).toHaveBeenCalledTimes(1)
    const [path, sent] = apiPatch.mock.calls[0]! as [string, Record<string, unknown>]
    expect(path).toBe('/api/tasks/ship')
    expect(sent.status).toBe('in_review')
    expect(sent.expected_revision).toBe(NEXT_REVISION)
    // And the control follows the stored record rather than snapping back to the
    // status the dialog was opened on.
    expect(wrapper.get('[data-status="in_review"]').attributes('aria-checked')).toBe('true')
    expect(wrapper.get('[data-status="backlog"]').attributes('aria-checked')).toBe('false')
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

    await wrapper.get('[data-status="done"]').trigger('click')
    await flushPromises()
    await nextTick()

    expect(apiPatch).not.toHaveBeenCalled()
    expect(apiPost).toHaveBeenCalledWith('/api/tasks/ship/complete', {
      workspace: 'personal',
      expected_revision: NEXT_REVISION,
    })
    expect(wrapper.get('[data-status="done"]').attributes('aria-checked')).toBe('true')
    wrapper.unmount()
  })

  it('walks the dialog\'s status radios with the arrow keys, writing each move', async () => {
    const wrapper = await mountBoard()
    apiGet.mockResolvedValue(detailAnswer(task(), ''))
    apiPatch.mockResolvedValue({
      workspace: 'personal',
      task: { ...task({ status: 'in_progress' }), revision: THIRD_REVISION, body: '' },
    })
    await card(wrapper, 'Ship the board').get('.task-open').trigger('click')
    await flushPromises()
    await nextTick()

    const group = wrapper.get('[role="radiogroup"]')
    // One tab stop: the checked radio.
    expect(group.findAll('[tabindex="0"]').map((r) => r.text())).toEqual(['To do'])
    await group.trigger('keydown', { key: 'ArrowRight' })
    await flushPromises()
    await nextTick()
    expect((apiPatch.mock.calls[0]![1] as Record<string, unknown>).status).toBe('in_progress')
    expect(wrapper.get('[data-status="in_progress"]').attributes('aria-checked')).toBe('true')
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
    expect(wrapper.get('.task-body').text()).toBe('Prose belonging to Wire the store')
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
    await dragCard(wrapper, 'Ship the board', 1)

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
        .toEqual(['To do', 'In progress', 'In review', 'Done'])
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
   * The `POST …/attempt/{id}/update` answer, as the service writes it: the attempt
   * rebound to the revision the update was made at, and the row carrying the flag
   * that rebind just cleared.
   */
  function updateAnswer(edited: Task) {
    return {
      workspace: 'personal',
      updated: true,
      queued: false,
      chat_id: edited.chat_id,
      attempt: {
        attempt_id: LIVE_ID, task_id: edited.id, task_revision: edited.revision,
        chat_id: edited.chat_id, state: 'running',
        created_at: '2026-03-02T09:00:00+00:00',
        updated_at: '2026-03-02T09:01:00+00:00', ended_at: '', detail: '', live: true,
      },
      task: { ...edited, revision: NEXT_REVISION, changed_since_delegated: false },
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

  /** A control in the card's editor, opening the editor first. */
  async function editorControl(wrapper: ReturnType<typeof mount>, title: string, label: string) {
    await card(wrapper, title).get('.task-open').trigger('click')
    await flushPromises()
    await nextTick()
    return wrapper.findAll('.task-delegate-actions .task-chip').find((c) => c.text() === label)!
  }

  /**
   * Open the delegation preview for a card: the card's editor, then its Agent
   * block's Delegate. The card itself carries no Delegate — handing over starts
   * where the preview is.
   */
  async function openPreview(wrapper: ReturnType<typeof mount>, title: string) {
    await card(wrapper, title).get('.task-open').trigger('click')
    await flushPromises()
    await nextTick()
    await wrapper.get('.task-delegate-actions .task-chip').trigger('click')
    await flushPromises()
    await nextTick()
  }

  it('draws the attempt badge for every state, coloured by what it means',
    async () => {
      const wrapper = await mountWithBody([
        task({ id: 'a', title: 'Never delegated' }),
        liveTask(),
        task({
          id: 'b', title: 'Waiting on you', status: 'in_review',
          attempt_state: 'needs_you', live_attempt_id: 'c'.repeat(32),
        }),
        task({
          id: 'c', title: 'Finished', status: 'done',
          attempt_state: 'ready_for_review', live_attempt_id: 'd'.repeat(32),
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
      // And a finished turn is a badge, never a Done the board moved on its own.
      // Scoped to the meta row: this card is also *inconsistent* (it sits in Done
      // while its attempt still holds it), and that finding carries its own badge.
      expect(badge('Finished').text()).toBe('Review ready')
      expect(card(wrapper, 'Finished').findAll('.task-meta .badge')).toHaveLength(1)
      // A task nobody delegated has no badge at all.
      expect(card(wrapper, 'Never delegated').findAll('.task-meta .badge'))
        .toHaveLength(0)
      wrapper.unmount()
    })

  it('says the result was reached against an older description', async () => {
    const wrapper = await mountWithBody([
      liveTask(),
      task({
        id: 'b', title: 'Waiting on you', status: 'in_review',
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
      expect(labels('Wire the store')).toEqual(['Chat', 'Stop', 'Detach'])
      // A live attempt holds the task, so there is no Done to tick.
      expect(card(wrapper, 'Wire the store').find('.task-check').exists()).toBe(true)
      // A task with nothing delegated has no attempt gestures and no Chat: the
      // board only links a chat it was told about, and handing over starts in
      // the editor. Its one gesture is the Done checkbox.
      expect(labels('Never delegated')).toEqual([])
      expect(card(wrapper, 'Never delegated').find('.task-check').exists()).toBe(true)
      wrapper.unmount()
    })

  it('offers a released review a delegation, never the live controls', async () => {
    // The user approved the result or detached the card, so the attempt is out of
    // the live set while its badge still reads `ready_for_review`. Drawn from the
    // badge this card would show Stop and Detach over a task with no chat to open
    // and no way to hand it over again — `live_attempt_id` is what says otherwise.
    const released = task({
      id: 'released',
      title: 'Reviewed, then released',
      status: 'in_progress',
      assignee: 'agent',
      chat_id: '',
      attempt_id: '',
      attempt_state: 'ready_for_review',
      live_attempt_id: '',
      revision: NEXT_REVISION,
    })
    const wrapper = await mountWithBody([released])

    const labels = card(wrapper, 'Reviewed, then released')
      .findAll('.task-chip').map((c) => c.text())
    // No attempt gestures, and a plain Done checkbox because there is nothing
    // left to approve.
    expect(labels).toEqual([])
    expect(card(wrapper, 'Reviewed, then released').find('.task-check').exists()).toBe(true)

    // And the editor agrees: the preview opens as a first hand-over.
    await card(wrapper, 'Reviewed, then released').get('.task-open').trigger('click')
    await flushPromises()
    await nextTick()
    const actions = wrapper.findAll('.task-delegate-actions .btn-chip').map((c) => c.text())
    expect(actions, 'no Delegate control for a released attempt').toContain('Delegate to agent')
    // No History control: the attempts are listed inline in the same block.
    expect(actions).not.toContain('History')
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
        .toEqual(['Chat', 'Resume', 'Retry'])

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
        .toEqual(['Chat', 'Resume', 'Retry'])
      wrapper.unmount()
    })

  it('approves a reviewed result into Done in one gesture, and keeps the attempt',
    async () => {
      // The review is the case the issue describes, and Approve Done is the button
      // for it: one request, no Detach first, and the service releases the linkage
      // and completes together so the attempt stays as review-ready history rather
      // than settling as `stopped` and losing the result.
      const reviewed = liveTask({
        attempt_state: 'ready_for_review', revision: THIRD_REVISION,
      })
      apiPost.mockResolvedValue({
        workspace: 'personal',
        task: {
          ...reviewed,
          status: 'done',
          chat_id: '',
          attempt_id: '',
          attempt_state: 'ready_for_review',
          live_attempt_id: '',
          revision: NEXT_REVISION,
        },
      })
      const wrapper = await mountWithBody([reviewed])

      // No Stop and no Detach on a card whose turn has ended: there is nothing
      // running to stop, and Detach-then-Done is the workaround this replaces.
      const labels = card(wrapper, 'Wire the store').findAll('.task-chip').map((c) => c.text())
      expect(labels).toEqual(['Chat', 'Approve Done'])

      await chip(wrapper, 'Wire the store', 'Approve Done').trigger('click')
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
      // The approved card is `ready_for_review` again with ``
      // and no live attempt — the exact shape that used to raise a false
      // "result without a badge" flag on every approved or detached review.
      const approvedCard = wrapper.findAll('.task-card')
        .find((c) => c.text().includes('Wire the store'))!
      expect(approvedCard.find('.task-reconcile').exists()).toBe(false)
      wrapper.unmount()
    })

  it('points a review-ready card at the result and approves it from the editor too',
    async () => {
      const reviewed = liveTask({
        attempt_state: 'ready_for_review', revision: THIRD_REVISION,
        attempt_outcome: 'done', attempt_summary: 'Wired the store; tests in store.test.ts.',
      })
      apiPost.mockResolvedValue({
        workspace: 'personal',
        task: { ...reviewed, status: 'done', revision: NEXT_REVISION },
      })
      const wrapper = await mountWithBody([reviewed])

      // The agent's own word, and its own summary of what it did.
      const shown = card(wrapper, 'Wire the store')
      expect(shown.get('.badge').text()).toBe('Agent says done')
      expect(shown.get('.task-agent-note').text()).toBe('Wired the store; tests in store.test.ts.')

      await shown.get('.task-open').trigger('click')
      await flushPromises()
      await nextTick()
      const control = wrapper.findAll('.task-delegate-actions .btn-chip')
        .find((c) => c.text() === 'Approve Done')
      expect(control, 'Approve Done in the editor').toBeTruthy()
      await control!.trigger('click')
      await flushPromises()

      expect(apiPost).toHaveBeenCalledWith('/api/tasks/doing/complete', {
        workspace: 'personal',
        expected_revision: THIRD_REVISION,
      })
      wrapper.unmount()
    })

  it('offers Send update for an edit made under the agent, and sends it as one message',
    async () => {
      const edited = liveTask({
        changed_since_delegated: true,
        attempt_state: 'ready_for_review',
        revision: THIRD_REVISION,
      })
      // The answer the server gives: the attempt rebound to the revision the
      // update was made at, so `changed_since_delegated` comes back false and the
      // control retires on its own.
      apiPost.mockResolvedValue(updateAnswer(edited))
      const sendMessage = vi.fn((_chatId: string, _text: string) => true)
      useProjectStore().sendMessage = sendMessage as never
      const wrapper = await mountWithBody([edited], 'Wire the store with the new owner.')

      await chip(wrapper, 'Wire the store', 'Send update').trigger('click')
      await flushPromises()
      await nextTick()

      const sheet = wrapper.get('.task-sheet')
      expect(sheet.text()).toContain('does not delegate again')
      // The exact text, before it is sent: the user is putting words into a chat
      // they may not have open.
      expect(sheet.get('.task-update-text').text()).toContain('Wire the store with the new owner.')
      expect(sheet.get('.btn-primary').text()).toBe('Send update')

      await sheet.get('.btn-primary').trigger('click')
      await flushPromises()

      // One ordinary message into the attempt's own chat, through the board rather
      // than the composer — the server is what rebinds the attempt, which a
      // composer send cannot do. And the composer was not used at all.
      expect(apiPost).toHaveBeenCalledTimes(1)
      const [url, body] = apiPost.mock.calls[0] as [string, Record<string, string>]
      expect(url).toBe(`/api/tasks/doing/attempt/${LIVE_ID}/update`)
      expect(body.workspace).toBe('personal')
      expect(body.expected_revision).toBe(THIRD_REVISION)
      expect(body.message).toContain('Wire the store with the new owner.')
      expect(sendMessage).not.toHaveBeenCalled()
      wrapper.unmount()
    })

  it('retires the Send update control and says the update landed', async () => {
    const edited = liveTask({
      changed_since_delegated: true,
      attempt_state: 'ready_for_review',
      revision: THIRD_REVISION,
    })
    apiPost.mockResolvedValue(updateAnswer(edited))
    const wrapper = await mountWithBody([edited], 'Wire the store with the new owner.')

    await chip(wrapper, 'Wire the store', 'Send update').trigger('click')
    await flushPromises()
    await nextTick()
    await wrapper.get('.task-sheet .btn-primary').trigger('click')
    await flushPromises()
    await nextTick()

    // The sheet is the decision, and the decision is made: it closes on the answer.
    expect(wrapper.find('.task-sheet').exists()).toBe(false)
    const shown = card(wrapper, 'Wire the store')
    // The rebind cleared the flag the button reads, so the button is gone…
    expect(shown.findAll('.task-chip').map((c) => c.text())).not.toContain('Send update')
    expect(shown.find('.task-changed').exists()).toBe(false)
    // …and the card says what happened, because a row that simply looks untouched
    // cannot tell the user whether the send landed.
    const note = shown.get('.task-update-sent')
    expect(note.text()).toContain('Update sent')
    expect(note.attributes('role')).toBe('status')
    wrapper.unmount()
  })

  it('does not carry a sent confirmation to a colliding workspace task', async () => {
    const edited = liveTask({ changed_since_delegated: true })
    apiPost.mockResolvedValue(updateAnswer(edited))
    const wrapper = await mountWithBody([edited])
    await chip(wrapper, 'Wire the store', 'Send update').trigger('click')
    await flushPromises()
    await wrapper.get('.task-sheet .btn-primary').trigger('click')
    await flushPromises()
    expect(wrapper.find('.task-update-sent').exists()).toBe(true)
    apiGet.mockResolvedValue({ workspace: 'work', tasks: [edited] })
    useProjectStore().activeWorkspace = 'work'
    await flushPromises()
    expect(card(wrapper, 'Wire the store').exists()).toBe(true)
    expect(wrapper.find('.task-update-sent').exists()).toBe(false)
    wrapper.unmount()
  })

  it.each(['success', 'refusal'])('drops a late update %s without altering a new sheet', async (result) => {
    const edited = liveTask({ changed_since_delegated: true })
    let resolve = (_answer: unknown) => {}
    let reject = (_error: unknown) => {}
    apiPost.mockImplementation(() => new Promise((yes, no) => { resolve = yes; reject = no }))
    const wrapper = await mountWithBody([edited])
    await chip(wrapper, 'Wire the store', 'Send update').trigger('click')
    await flushPromises()
    await wrapper.get('.task-sheet .btn-primary').trigger('click')
    expect(wrapper.get('.task-sheet .btn-primary').attributes('disabled')).toBeDefined()
    apiGet.mockImplementation((url: string) => Promise.resolve(url.includes('/api/tasks?')
      ? { workspace: 'work', tasks: [edited] }
      : { workspace: 'work', task: { ...edited, body: 'Work description' } }))
    useProjectStore().activeWorkspace = 'work'
    await flushPromises()
    expect(wrapper.find('.task-sheet').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('Sending…')
    await chip(wrapper, 'Wire the store', 'Send update').trigger('click')
    await flushPromises()
    expect(wrapper.get('.task-sheet .btn-primary').attributes('disabled')).toBeUndefined()
    if (result === 'success') resolve(updateAnswer(edited))
    else reject(new Error('Late refusal'))
    await flushPromises()
    expect(wrapper.get('.task-sheet').text()).toContain('Work description')
    expect(wrapper.get('.task-sheet .btn-primary').attributes('disabled')).toBeUndefined()
    expect(wrapper.find('.task-update-sent').exists()).toBe(false)
    expect(wrapper.find('.task-action-error').exists()).toBe(false)
    wrapper.unmount()
  })

  it('shows the update in flight, then keeps the control when the send is refused',
    async () => {
      const edited = liveTask({
        changed_since_delegated: true,
        attempt_state: 'ready_for_review',
        revision: THIRD_REVISION,
      })
      let release = (_answer: unknown) => {}
      const refused = Object.assign(new Error('HTTP 409'), {
        payload: { error: { code: 'task_update_busy', message: 'that chat is busy' } },
      })
      apiPost.mockImplementation(
        () => new Promise((_resolve, reject) => { release = reject }),
      )
      const wrapper = await mountWithBody([edited], 'Wire the store with the new owner.')

      await chip(wrapper, 'Wire the store', 'Send update').trigger('click')
      await flushPromises()
      await nextTick()
      await wrapper.get('.task-sheet .btn-primary').trigger('click')
      await nextTick()

      // In flight, said on the control rather than left looking like a no-op, and
      // the confirm cannot be pressed twice.
      expect(card(wrapper, 'Wire the store').findAll('.task-chip').map((c) => c.text()))
        .toContain('Sending…')
      expect(wrapper.get('.task-sheet .btn-primary').attributes('disabled')).toBeDefined()

      release(refused)
      await flushPromises()
      await nextTick()

      // Refused: the flag stands, the control stays, and the server's own sentence
      // is what says why — the user can try again.
      expect(wrapper.get('.task-sheet .task-action-error').text()).toContain('that chat is busy')
      expect(card(wrapper, 'Wire the store').findAll('.task-chip').map((c) => c.text()))
        .toContain('Send update')
      expect(card(wrapper, 'Wire the store').find('.task-update-sent').exists()).toBe(false)
      wrapper.unmount()
    })

  it('does not offer Send update until the edit is real', async () => {
      // A clean settle reads `changed_since_delegated: false`, because the watcher
    // rebinds the attempt to the revision its own review flag left behind. A card
    // with no warning has no update to send, and a button that would re-send an
    // unchanged description is the silent re-delegation the board rules out.
    const wrapper = await mountWithBody([
      liveTask({ attempt_state: 'ready_for_review' }),
    ])

    const labels = card(wrapper, 'Wire the store').findAll('.task-chip').map((c) => c.text())
    expect(labels).not.toContain('Send update')
    expect(card(wrapper, 'Wire the store').find('.task-changed').exists()).toBe(false)
    wrapper.unmount()
  })

  it('reads a task\'s attempts as history, with each one\'s chat and ending',
    async () => {
      const reviewed = liveTask({
        attempt_state: 'ready_for_review', revision: THIRD_REVISION,
      })
      const attemptsAnswer = {
        workspace: 'personal',
        task: { ...reviewed, body: 'Wire the store.' },
        attempts: [
          {
            attempt_id: LIVE_ID, task_id: 'doing', task_revision: THIRD_REVISION,
            chat_id: 'chat-7', state: 'ready_for_review',
            created_at: '2026-03-02T09:00:00+00:00',
            updated_at: '2026-03-02T09:04:00+00:00',
            ended_at: '2026-03-02T09:04:00+00:00', detail: '', live: true,
          },
          {
            attempt_id: SETTLED_ID, task_id: 'doing', task_revision: NEXT_REVISION,
            chat_id: 'chat-3', state: 'failed',
            created_at: '2026-03-01T09:00:00+00:00',
            updated_at: '2026-03-01T09:02:00+00:00',
            ended_at: '2026-03-01T09:02:00+00:00',
            detail: 'the turn ended in an error', live: false,
          },
        ],
      }
      apiGet.mockImplementation((url: string) => {
        if (url.includes('/attempts')) return Promise.resolve(attemptsAnswer)
        if (url.includes('/api/tasks?')) return Promise.resolve({ workspace: 'personal', tasks: [reviewed] })
        return Promise.resolve({ workspace: 'personal', task: { ...reviewed, body: 'Wire the store.' } })
      })
      const wrapper = mount(TaskBoardView, { attachTo: document.body })
      await flushPromises()
      await nextTick()

      // Nothing has asked for it yet: a workspace of retried tasks must not pay
      // for a history nobody opened.
      expect(apiGet).not.toHaveBeenCalledWith(expect.stringContaining('/attempts'))

      const switchChat = vi.fn()
      useProjectStore().switchChat = switchChat
      // Opening the editor reads them: the list is inline in its Agent block.
      await card(wrapper, 'Wire the store').get('.task-open').trigger('click')
      await flushPromises()
      await nextTick()

      expect(apiGet).toHaveBeenCalledWith('/api/tasks/doing/attempts?workspace=personal')
      const sheet = wrapper.get('.task-delegate')
      const rows = sheet.findAll('.task-history-row')
      expect(rows).toHaveLength(2)
      // Live one first, and each attempt keeps its own chat: a retry that replaced
      // this one has not erased where the old turn's answer lives.
      expect(rows[0]!.text()).toContain('Review ready')
      expect(rows[1]!.text()).toContain('Failed')
      expect(rows[1]!.text()).toContain('the turn ended in an error')
      // Each row wears its own attempt's colour, not the current attempt's: a
      // failed retry under a review-ready current one must not render green.
      expect(rows[0]!.find('.badge').classes()).toContain('badge--success')
      expect(rows[1]!.find('.badge').classes()).toContain('badge--error')
      await rows[1]!.get('button').trigger('click')
      expect(switchChat).toHaveBeenCalledWith('chat-3')

      // Re-opening re-reads: a turn may have reported since.
      await wrapper.get('.task-sheet-head .btn-icon').trigger('click')
      await flushPromises()
      await card(wrapper, 'Wire the store').get('.task-open').trigger('click')
      await flushPromises()
      expect(apiGet.mock.calls.filter((c) => String(c[0]).includes('/attempts'))).toHaveLength(2)
      wrapper.unmount()
    })

  it('flags a row that disagrees with itself instead of rewriting it', async () => {
    // A card whose attempt ended while it still reads In progress for the agent.
    // The board says so and names the controls; it never settles the attempt or
    // moves the card on the user's behalf.
    const wrapper = await mountWithBody([settledTask()])

    const notes = card(wrapper, 'Broke halfway').findAll('.task-reconcile-note')
    expect(notes).toHaveLength(1)
    expect(notes[0]!.text()).toContain('its last attempt ended interrupted')
    expect(notes[0]!.text()).toContain('Resume continues that attempt')
    // And nothing was written to "fix" it.
    expect(apiPost).not.toHaveBeenCalled()
    expect(apiPatch).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('does not flag an approved or detached review as a result without a badge',
    async () => {
      // A released `ready_for_review` attempt keeps that state forever — the user
      // already approved it or detached the card — so it must not read as "a
      // result waiting with no Review badge". Only a *live* attempt can be that.
      const approved = task({
        id: 'approved', title: 'Approved result',
        status: 'done', assignee: 'agent',
        attempt_state: 'ready_for_review',
        live_attempt_id: '',
      })
      const wrapper = await mountWithBody([approved])
      expect(card(wrapper, 'Approved result').find('.task-reconcile').exists()).toBe(false)
      wrapper.unmount()
    })

  it('flags a chat the browser cannot see, and stays quiet before it has looked',
    async () => {
      const store = useProjectStore()
      // No chats loaded: the board knows nothing, and a flag on that basis would
      // be a guess about every card.
      const wrapper = await mountWithBody([liveTask()])
      expect(card(wrapper, 'Wire the store').find('.task-reconcile').exists()).toBe(false)
      wrapper.unmount()

      // Once the sidebar has the list, a card naming a chat that is not in it is
      // a real thing to say — and it says it as what it can see, not as a verdict.
      store.chats = [{ chat_id: 'chat-other', project_id: 'p1' } as never]
      const named = await mountWithBody([liveTask()])
      const notes = card(named, 'Wire the store').findAll('.task-reconcile-note')
      expect(notes).toHaveLength(1)
      expect(notes[0]!.text()).toContain('not in this browser')
      expect(notes[0]!.text()).toContain('Detach releases the card')
      named.unmount()
    })

  it('closes the workspace\'s history when the user switches away', async () => {
    const store = useProjectStore()
    apiGet.mockImplementation((url: string) => {
      if (url.includes('workspace=work')) return Promise.resolve({ workspace: 'work', tasks: [] })
      if (url.includes('/api/tasks?')) {
        return Promise.resolve({ workspace: 'personal', tasks: [liveTask()] })
      }
      return Promise.resolve({ workspace: 'personal', task: { ...liveTask(), body: 'Wire the store.' } })
    })
    const wrapper = mount(TaskBoardView, { attachTo: document.body })
    await flushPromises()
    await nextTick()

    await card(wrapper, 'Wire the store').get('.task-open').trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.findAll('.task-sheet').length).toBeGreaterThan(0)

    store.activeWorkspace = 'work'
    await flushPromises()
    await nextTick()
    // The attempts held belonged to the workspace the user left; leaving the sheet
    // open would offer them as this one's.
    expect(wrapper.find('.task-sheet').exists()).toBe(false)
    wrapper.unmount()
  })

  it('calls a turn that ended without a report unfinished, and says why', async () => {
    const wrapper = await mountWithBody([liveTask({
      attempt_state: 'needs_you', attempt_detail: 'the turn ended without a report from the agent',
    })])
    const shown = card(wrapper, 'Wire the store')
    expect(shown.get('.badge').text()).toBe('Unfinished')
    expect(shown.get('.task-agent-note').text()).toContain('without a report')
    wrapper.unmount()
  })

  it('offers to continue an archived chat\'s attempt in a new chat, never to resume it', async () => {
    useProjectStore().chats = [{ chat_id: 'chat-7', project_id: 'p1', archived: true } as never]
    const wrapper = await mountWithBody([settledTask({
      attempt_outcome: 'blocked', attempt_summary: 'Schema done; need creds.',
      attempt_detail: 'the chat was archived',
    })])
    const shown = card(wrapper, 'Broke halfway')
    const labels = shown.findAll('.task-chip').map((c) => c.text())
    expect(labels).not.toContain('Resume')
    expect(labels).toContain('Continue in new chat')
    expect(shown.text()).toContain('The chat was archived')
    expect(shown.get('.task-agent-note').text()).toBe('Schema done; need creds.')

    await shown.get('.task-open').trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.get('.task-delegate-actions .task-chip').text()).toBe('Continue in a new chat')
    wrapper.unmount()
  })

  it('makes a URL in a title a link that opens without opening the card', async () => {
    const wrapper = await mountWithBody([plainTask({
      title: 'Check https://www.remotion.dev/docs/ai/skills/ today',
    })])
    const shown = card(wrapper, 'remotion.dev')
    const link = shown.get('.task-title a')
    expect(link.attributes('href')).toBe('https://www.remotion.dev/docs/ai/skills/')
    expect(link.attributes('target')).toBe('_blank')
    expect(link.attributes('rel')).toContain('noopener')
    expect(link.text()).toBe('remotion.dev/docs/ai/skills')
    await link.trigger('click')
    await nextTick()
    expect(wrapper.find('#task-detail-title').exists()).toBe(false)
    // The keyboard's way in still carries the whole title.
    expect(shown.get('.task-open').attributes('aria-label')).toBe(
      'Edit Check https://www.remotion.dev/docs/ai/skills/ today',
    )
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

      await (await editorControl(wrapper, 'Draft the runbook', 'Delegate to agent')).trigger('click')
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
      await (await editorControl(wrapper, 'Draft the runbook', 'Delegate to agent')).trigger('click')
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
