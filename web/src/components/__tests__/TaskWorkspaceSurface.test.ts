// @vitest-environment jsdom

/**
 * The shared task surface's overlay mode.
 *
 * The board itself is covered by `TaskBoardView.test.ts`. What only a real
 * mount of this mode can decide: that the same editor (not a read-only peek)
 * is drawn over a host without the board or an address change; that the first
 * read, its failure and a missing task are their own states; that a close the
 * host awaits cannot drop an invalid or refused draft; and that the internal
 * close before the delegation preview keeps the surface mounted.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick, reactive } from 'vue'
import { routeLocationKey, routerKey } from 'vue-router'
import TaskWorkspaceSurface from '../TaskWorkspaceSurface.vue'
import { useProjectStore } from '../../stores/projects'
import { useTaskSignalsStore } from '../../stores/taskSignals'
import type { Task, TaskRow } from '../../lib/types'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())
const apiPatch = vi.hoisted(() => vi.fn())
const apiDel = vi.hoisted(() => vi.fn())
const askConfirm = vi.hoisted(() => vi.fn(
  async (_message?: string, _options?: { title?: string }) => true,
))
const askConfirmReason = vi.hoisted(() => vi.fn(
  async (_message?: string, _options?: { title?: string }) => ({ confirmed: true, reason: '' }),
))
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
const THIRD_REVISION = 'c'.repeat(64)
const FOURTH_REVISION = 'e'.repeat(64)
const LIVE_ID = 'd'.repeat(32)

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
    completed_at: overrides.status === 'done' ? '2026-03-01T08:00:00Z' : null,
    has_resolution: false,
    ...overrides,
  }
}

const BOARD: TaskRow[] = [
  task(),
  task({ id: 'doing', title: 'Wire the store', status: 'in_progress' }),
  task({ id: 'held', title: 'Wait on a key', status: 'in_review' }),
]

/** A `GET /api/tasks/{id}` answer for a row, with a real `body`. */
function detailAnswer(row: Task, body = '') {
  return { workspace: 'personal', task: { ...row, revision: NEXT_REVISION, body } }
}

/** List reads answer with `rows`; one task's read answers with its own record. */
function answerWith(rows: TaskRow[], body = '') {
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
 * Mount the surface in overlay mode with a route the host is showing.
 *
 * The route is a plain reactive location, not a router: the point of the test
 * is that opening and closing this surface never replaces it.
 */
function mountOverlay(taskId = 'ship') {
  const route = reactive({ fullPath: '/chat/abc', params: {} as Record<string, string> })
  const replace = vi.fn()
  const wrapper = mount(TaskWorkspaceSurface, {
    attachTo: document.body,
    props: { mode: 'overlay' as const, taskId },
    global: {
      provide: {
        [routeLocationKey as symbol]: route,
        [routerKey as symbol]: { replace, afterEach: vi.fn() },
      },
    },
  })
  return { wrapper, route, replace }
}

const requestClose = (wrapper: ReturnType<typeof mount>) =>
  (wrapper.vm as unknown as { requestClose: () => Promise<boolean> }).requestClose()

const editor = (wrapper: ReturnType<typeof mount>) => wrapper.find('.task-detail-sheet')

const overlayState = (wrapper: ReturnType<typeof mount>) => wrapper.find('.task-overlay-state')

/** The description's editor: a described task opens rendered, so click it first. */
async function editBody(wrapper: ReturnType<typeof mount>) {
  if (!wrapper.find('#task-detail-body').exists()) {
    await wrapper.get('.task-body').trigger('click')
    await nextTick()
  }
  return wrapper.get<HTMLTextAreaElement>('#task-detail-body')
}

/**
 * Move the drawn row to `revision` through the app-wide task refresh.
 *
 * The list read carries the new revision while the board's description slot
 * stays at the revision the editor last read — exactly the state the row
 * advancing under an open editor produces.
 */
async function advanceRowRevision(revision: string, body: string) {
  apiGet.mockImplementation((url: string) => {
    if (url.startsWith('/api/tasks?')) {
      return Promise.resolve({
        workspace: 'personal',
        tasks: BOARD.map((row) => (row.id === 'ship' ? { ...row, revision } : row)),
      })
    }
    const id = url.slice('/api/tasks/'.length).split('?')[0]!
    const row = BOARD.find((entry) => entry.id === id) ?? task({ id })
    return Promise.resolve({
      workspace: 'personal',
      task: {
        ...row,
        revision: id === 'ship' ? revision : ('revision' in row ? row.revision : ''),
        body: id === 'ship' ? body : '',
      },
    })
  })
  const signals = useTaskSignalsStore()
  signals.loadedWorkspace = 'personal'
  signals.tasks = [task({ revision })]
  await flushPromises()
  await nextTick()
}

describe('TaskWorkspaceSurface overlay mode', () => {
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
    Reflect.deleteProperty(navigator, 'clipboard')
    vi.restoreAllMocks()
  })

  it('draws the full editor over the host, with no board chrome and no URL change', async () => {
    answerWith(BOARD, 'Prose the list never carried.')
    const { wrapper, route, replace } = mountOverlay()
    await flushPromises()
    await nextTick()

    // The board, its filters and its create gesture belong to the route, not
    // to a surface mounted over another screen.
    expect(wrapper.find('.task-lane').exists()).toBe(false)
    expect(wrapper.find('.task-filters').exists()).toBe(false)
    expect(wrapper.find('.header-hamburger').exists()).toBe(false)
    expect(wrapper.find('.task-new').exists()).toBe(false)

    const sheet = editor(wrapper)
    expect(sheet.exists()).toBe(true)
    expect(sheet.attributes('role')).toBe('dialog')
    expect(sheet.attributes('aria-modal')).toBe('true')
    // The same editor, not a read-only peek: every field and the nested Agent
    // block are here.
    expect(sheet.get<HTMLTextAreaElement>('#task-detail-name').element.value).toBe('Ship the board')
    expect(sheet.find('[data-status="in_progress"]').exists()).toBe(true)
    expect(sheet.find('#task-detail-project').exists()).toBe(true)
    expect(sheet.find('#task-detail-due').exists()).toBe(true)
    // The description opens rendered, as it does in the board's editor, and is
    // one click from the same textarea.
    expect(sheet.get('.task-body').text()).toBe('Prose the list never carried.')
    await sheet.get('.task-body').trigger('click')
    await nextTick()
    expect(sheet.find('#task-detail-body').exists()).toBe(true)
    expect(sheet.get('.task-delegate-actions').text()).toContain('Delegate to agent')

    // Opening a task is not navigation, and closing it will not be either.
    expect(replace).not.toHaveBeenCalled()
    expect(route.fullPath).toBe('/chat/abc')

    expect(await requestClose(wrapper)).toBe(true)
    await flushPromises()
    await nextTick()
    expect(editor(wrapper).exists()).toBe(false)
    expect(replace).not.toHaveBeenCalled()
    expect(route.fullPath).toBe('/chat/abc')
    expect(wrapper.emitted('close')).toHaveLength(1)
    wrapper.unmount()
  })

  it('holds the loading state until the fresh board read answers', async () => {
    let answerBoard: (value: unknown) => void = () => {}
    apiGet.mockImplementation((url: string) => (
      url.startsWith('/api/tasks?')
        ? new Promise((resolve) => { answerBoard = resolve })
        : Promise.resolve(detailAnswer(task()))
    ))

    const { wrapper } = mountOverlay()
    await nextTick()
    expect(overlayState(wrapper).text()).toContain('Loading task')
    expect(editor(wrapper).exists()).toBe(false)

    answerBoard({ workspace: 'personal', tasks: BOARD })
    await flushPromises()
    await nextTick()
    expect(editor(wrapper).exists()).toBe(true)
    expect(overlayState(wrapper).exists()).toBe(false)
    wrapper.unmount()
  })

  it('closes a loading overlay, and a read that answers late does not open it', async () => {
    let answerBoard: (value: unknown) => void = () => {}
    apiGet.mockImplementation((url: string) => (
      url.startsWith('/api/tasks?')
        ? new Promise((resolve) => { answerBoard = resolve })
        : Promise.resolve(detailAnswer(task()))
    ))

    const { wrapper } = mountOverlay()
    await nextTick()
    expect(await requestClose(wrapper)).toBe(true)
    expect(wrapper.emitted('close')).toHaveLength(1)

    answerBoard({ workspace: 'personal', tasks: BOARD })
    await flushPromises()
    await nextTick()
    expect(editor(wrapper).exists()).toBe(false)
    expect(overlayState(wrapper).exists()).toBe(false)
    wrapper.unmount()
  })

  it('shows the failed read with a Retry instead of an editor over stale rows', async () => {
    let failed = true
    apiGet.mockImplementation((url: string) => {
      if (!url.startsWith('/api/tasks?')) return Promise.resolve(detailAnswer(task()))
      return failed
        ? Promise.reject(new Error('the vault is offline'))
        : Promise.resolve({ workspace: 'personal', tasks: BOARD })
    })

    const { wrapper } = mountOverlay()
    await flushPromises()
    await nextTick()

    expect(editor(wrapper).exists()).toBe(false)
    expect(overlayState(wrapper).text()).toContain('the vault is offline')
    // The failure is a state with a way forward, not a blank sheet.
    failed = false
    const retry = overlayState(wrapper).findAll('button').find((button) => button.text() === 'Retry')!
    await retry.trigger('click')
    await flushPromises()
    await nextTick()
    expect(editor(wrapper).exists()).toBe(true)
    expect(overlayState(wrapper).exists()).toBe(false)
    wrapper.unmount()
  })

  it('says the task is missing once the fresh board has answered', async () => {
    answerWith(BOARD)
    const { wrapper, replace } = mountOverlay('gone')
    await flushPromises()
    await nextTick()

    expect(editor(wrapper).exists()).toBe(false)
    expect(overlayState(wrapper).text()).toContain('This task is not on the personal board.')
    expect(replace).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('refuses a requested close while the title is blank, and writes nothing', async () => {
    answerWith(BOARD)
    const { wrapper } = mountOverlay()
    await flushPromises()
    await nextTick()

    await wrapper.get('#task-detail-name').setValue('   ')
    await nextTick()
    expect(wrapper.get('.task-saved').text()).toBe('Add a title to save.')

    expect(await requestClose(wrapper)).toBe(false)
    expect(editor(wrapper).exists()).toBe(true)
    expect(apiPatch).not.toHaveBeenCalled()
    expect(wrapper.emitted('close')).toBeUndefined()

    // The sheet's own × is the same request, so it cannot drop the draft either.
    await editor(wrapper).get('.task-sheet-head .btn-icon').trigger('click')
    await flushPromises()
    expect(editor(wrapper).exists()).toBe(true)
    expect(apiPatch).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('keeps the editor open on a refused save and closes once the write lands', async () => {
    answerWith(BOARD)
    apiPatch.mockRejectedValueOnce(Object.assign(new Error('HTTP 409'), {
      payload: { error: { code: 'task_revision_conflict', message: 'that task changed on disk', retryable: true } },
    }))
    const { wrapper } = mountOverlay()
    await flushPromises()
    await nextTick()

    await wrapper.get('#task-detail-name').setValue('Renamed')
    await nextTick()

    expect(await requestClose(wrapper)).toBe(false)
    expect(apiPatch).toHaveBeenCalledTimes(1)
    expect(apiPatch.mock.calls[0]![1]).toMatchObject({ title: 'Renamed', expected_revision: NEXT_REVISION })
    expect(editor(wrapper).exists()).toBe(true)
    expect(editor(wrapper).text()).toContain('that task changed on disk')
    expect(wrapper.emitted('close')).toBeUndefined()

    apiPatch.mockResolvedValue({
      workspace: 'personal',
      task: { ...task({ title: 'Renamed' }), revision: THIRD_REVISION, body: '' },
    })
    expect(await requestClose(wrapper)).toBe(true)
    await flushPromises()
    await nextTick()
    expect(editor(wrapper).exists()).toBe(false)
    expect(wrapper.emitted('close')).toHaveLength(1)
    wrapper.unmount()
  })

  it('closes the surface after the task is deleted', async () => {
    answerWith(BOARD)
    apiDel.mockResolvedValue({ workspace: 'personal', deleted: 'ship' })
    const { wrapper } = mountOverlay()
    await flushPromises()
    await nextTick()

    await editor(wrapper).get('.task-delete').trigger('click')
    await flushPromises()
    await nextTick()

    expect(apiDel).toHaveBeenCalledWith('/api/tasks/ship', {
      workspace: 'personal',
      expected_revision: NEXT_REVISION,
    })
    expect(editor(wrapper).exists()).toBe(false)
    expect(wrapper.emitted('close')).toHaveLength(1)
    wrapper.unmount()
  })

  it('closes the surface after a completion from the editor', async () => {
    answerWith(BOARD)
    apiPost.mockResolvedValue({
      workspace: 'personal',
      task: { ...task({ status: 'done' }), revision: NEXT_REVISION, body: '' },
    })
    const { wrapper } = mountOverlay()
    await flushPromises()
    await nextTick()

    await wrapper.get('[data-status="done"]').trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.find('.task-resolution-sheet').exists()).toBe(true)

    await wrapper.get('.task-resolution-sheet form').trigger('submit')
    await flushPromises()
    await nextTick()

    expect(apiPost).toHaveBeenCalledWith('/api/tasks/ship/complete', {
      workspace: 'personal',
      expected_revision: NEXT_REVISION,
      resolution: '',
    })
    expect(editor(wrapper).exists()).toBe(false)
    expect(wrapper.emitted('close')).toHaveLength(1)
    wrapper.unmount()
  })

  it('keeps the surface mounted across the internal close before the delegation preview', async () => {
    answerWith(BOARD, 'Write the runbook.')
    const { wrapper } = mountOverlay()
    await flushPromises()
    await nextTick()

    await editor(wrapper).get('.task-delegate-go').trigger('click')
    await flushPromises()
    await nextTick()

    // The editor closes so the preview is not drawn under a second modal; the
    // host is told that this is not the surface's final closure.
    expect(editor(wrapper).exists()).toBe(false)
    expect(wrapper.find('#task-delegate-title').exists()).toBe(true)
    expect(wrapper.emitted('detail-close')).toHaveLength(1)
    expect(wrapper.emitted('close')).toBeUndefined()

    const preview = wrapper.findAll('.task-sheet').find((sheet) => sheet.find('#task-delegate-title').exists())!
    const cancel = preview.findAll('button').find((button) => button.text() === 'Cancel')!
    await cancel.trigger('click')
    await flushPromises()
    await nextTick()

    // Cancelling returns to the editor the preview was opened from, still
    // over the same host.
    expect(wrapper.find('#task-delegate-title').exists()).toBe(false)
    expect(editor(wrapper).exists()).toBe(true)
    expect(wrapper.emitted('close')).toBeUndefined()
    wrapper.unmount()
  })

  it('closes the surface on a workspace switch, without writing the old draft', async () => {
    answerWith(BOARD)
    const { wrapper } = mountOverlay()
    await flushPromises()
    await nextTick()

    await wrapper.get('#task-detail-name').setValue('Renamed on personal')
    await nextTick()

    const store = useProjectStore()
    apiGet.mockImplementation((url: string) => Promise.resolve(
      url.includes('workspace=work')
        ? { workspace: 'work', tasks: [] }
        : { workspace: 'personal', tasks: BOARD },
    ))
    store.activeWorkspace = 'work'
    await flushPromises()
    await nextTick()

    // The draft belongs to the workspace being left; it is never presented to
    // the new one, and the surface does not keep drawing the old task.
    expect(apiPatch).not.toHaveBeenCalled()
    expect(editor(wrapper).exists()).toBe(false)
    expect(overlayState(wrapper).exists()).toBe(false)
    expect(wrapper.text()).not.toContain('Renamed on personal')
    expect(wrapper.emitted('close')).toHaveLength(1)
    wrapper.unmount()
  })

  it('replaces the editor when the host names another task, without a final close', async () => {
    answerWith(BOARD)
    const { wrapper } = mountOverlay()
    await flushPromises()
    await nextTick()
    expect(wrapper.get<HTMLTextAreaElement>('#task-detail-name').element.value).toBe('Ship the board')

    await wrapper.setProps({ taskId: 'doing' })
    await flushPromises()
    await nextTick()

    expect(wrapper.get<HTMLTextAreaElement>('#task-detail-name').element.value).toBe('Wire the store')
    expect(wrapper.emitted('detail-close')).toHaveLength(1)
    expect(wrapper.emitted('close')).toBeUndefined()
    wrapper.unmount()
  })

  it('keeps an invalid title and its form when the host replaces the task', async () => {
    answerWith(BOARD)
    const { wrapper } = mountOverlay()
    await flushPromises()
    await nextTick()

    await wrapper.get('#task-detail-name').setValue('   ')
    await nextTick()
    await wrapper.setProps({ taskId: 'doing' })
    await flushPromises()
    await nextTick()

    // The internal close that a replacement performs is not a discard: the
    // form stays as typed, says why, and the old task is not swapped for the
    // new one behind a draft that cannot be written.
    const sheet = editor(wrapper)
    expect(sheet.exists()).toBe(true)
    expect(sheet.get<HTMLTextAreaElement>('#task-detail-name').element.value).toBe('   ')
    expect(sheet.text()).toContain('Add a title before closing')
    expect(wrapper.emitted('detail-close')).toBeUndefined()
    expect(wrapper.emitted('close')).toBeUndefined()
    expect(apiPatch).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('refuses the internal close before the delegation preview while the title is blank', async () => {
    answerWith(BOARD, 'Write the runbook.')
    const { wrapper } = mountOverlay()
    await flushPromises()
    await nextTick()

    await wrapper.get('#task-detail-name').setValue('   ')
    await nextTick()
    await editor(wrapper).get('.task-delegate-go').trigger('click')
    await flushPromises()
    await nextTick()

    expect(editor(wrapper).exists()).toBe(true)
    expect(wrapper.get<HTMLTextAreaElement>('#task-detail-name').element.value).toBe('   ')
    expect(editor(wrapper).text()).toContain('Add a title before closing')
    expect(wrapper.find('#task-delegate-title').exists()).toBe(false)
    expect(wrapper.emitted('detail-close')).toBeUndefined()
    expect(apiPatch).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('defers a host replacement while the delegation preview is open, then applies it', async () => {
    answerWith(BOARD, 'Write the runbook.')
    const { wrapper } = mountOverlay()
    await flushPromises()
    await nextTick()

    await editor(wrapper).get('.task-delegate-go').trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.find('#task-delegate-title').exists()).toBe(true)
    expect(wrapper.emitted('detail-close')).toHaveLength(1)

    // A replacement requested now must not race the sheet's internal close or
    // discard the preview; it waits until the sheet is closed.
    await wrapper.setProps({ taskId: 'doing' })
    await flushPromises()
    await nextTick()
    expect(wrapper.find('#task-delegate-title').exists()).toBe(true)
    expect(editor(wrapper).exists()).toBe(false)
    expect(wrapper.emitted('close')).toBeUndefined()

    const preview = wrapper.findAll('.task-sheet').find((sheet) => sheet.find('#task-delegate-title').exists())!
    const cancel = preview.findAll('button').find((button) => button.text() === 'Cancel')!
    await cancel.trigger('click')
    await flushPromises()
    await nextTick()

    // Once the sheet is safely closed, the host's replacement is resolved.
    expect(wrapper.find('#task-delegate-title').exists()).toBe(false)
    expect(wrapper.get<HTMLTextAreaElement>('#task-detail-name').element.value).toBe('Wire the store')
    expect(wrapper.emitted('detail-close')).toHaveLength(2)
    expect(wrapper.emitted('close')).toBeUndefined()
    wrapper.unmount()
  })

  it('finishes the overlay when Open chat closes the editor and opens the chat', async () => {
    answerWith([
      task({
        id: 'ship',
        attempt_id: LIVE_ID,
        live_attempt_id: LIVE_ID,
        attempt_state: 'running',
        chat_id: 'c1',
      }),
    ], 'Write the runbook.')
    const store = useProjectStore()
    const switchChat = vi.spyOn(store, 'switchChat').mockResolvedValue(undefined)

    const { wrapper, route, replace } = mountOverlay()
    await flushPromises()
    await nextTick()
    expect(editor(wrapper).get('.task-delegate-go').text()).toBe('Open chat')

    await editor(wrapper).get('.task-delegate-go').trigger('click')
    await flushPromises()
    await nextTick()

    // Open chat is not a preview: it navigates, and the overlay must tell the
    // host it is finished instead of leaving an invisible mounted surface.
    expect(switchChat).toHaveBeenCalledWith('c1')
    expect(editor(wrapper).exists()).toBe(false)
    expect(wrapper.find('#task-delegate-title').exists()).toBe(false)
    expect(overlayState(wrapper).exists()).toBe(false)
    expect(wrapper.emitted('close')).toHaveLength(1)
    expect(replace).not.toHaveBeenCalled()
    expect(route.fullPath).toBe('/chat/abc')
    wrapper.unmount()
  })

  it('keeps a completion draft when a blank title refuses the requested close', async () => {
    answerWith(BOARD)
    const { wrapper } = mountOverlay()
    await flushPromises()
    await nextTick()

    // A user can blank the title before pressing Done; the completion sheet is
    // the last thing they typed, so a refused close must not erase it.
    await wrapper.get('#task-detail-name').setValue('   ')
    await wrapper.get('[data-status="done"]').trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.find('.task-resolution-sheet').exists()).toBe(true)
    await wrapper.get('#task-resolution-text').setValue('Checked the runbook end to end.')
    await nextTick()

    expect(await requestClose(wrapper)).toBe(false)
    expect(wrapper.find('.task-resolution-sheet').exists()).toBe(true)
    expect(wrapper.get<HTMLTextAreaElement>('#task-resolution-text').element.value)
      .toBe('Checked the runbook end to end.')
    expect(editor(wrapper).text()).toContain('Add a title before closing')
    expect(apiPost).not.toHaveBeenCalled()

    // With a title the close goes through, discarding the sheet without
    // completing anything.
    await wrapper.get('#task-detail-name').setValue('Ship the board')
    await nextTick()
    expect(await requestClose(wrapper)).toBe(true)
    await flushPromises()
    await nextTick()
    expect(wrapper.find('.task-resolution-sheet').exists()).toBe(false)
    expect(editor(wrapper).exists()).toBe(false)
    expect(apiPost).not.toHaveBeenCalled()
    expect(wrapper.emitted('close')).toHaveLength(1)
    wrapper.unmount()
  })

  it('refuses a completion while the editor holds a blank title', async () => {
    answerWith(BOARD)
    const { wrapper } = mountOverlay()
    await flushPromises()
    await nextTick()

    await wrapper.get('#task-detail-name').setValue('   ')
    await wrapper.get('[data-status="done"]').trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.find('.task-resolution-sheet').exists()).toBe(true)

    await wrapper.get('#task-resolution-text').setValue('Checked the runbook.')
    await wrapper.get('.task-resolution-sheet form').trigger('submit')
    await flushPromises()
    await nextTick()

    // Completing would close the editor behind the sheet; the unsaveable title
    // refuses that close, so the write must not happen either.
    expect(apiPost).not.toHaveBeenCalled()
    expect(wrapper.find('.task-resolution-sheet').exists()).toBe(true)
    expect(wrapper.get('.task-resolution-sheet').text()).toContain('Add a title before closing')
    expect(editor(wrapper).text()).toContain('Add a title before closing')
    wrapper.unmount()
  })

  it('keeps a completion note when the editor save is refused during the close', async () => {
    answerWith(BOARD)
    apiPatch.mockRejectedValueOnce(Object.assign(new Error('HTTP 409'), {
      payload: { error: { code: 'task_revision_conflict', message: 'that task changed on disk', retryable: true } },
    }))
    const { wrapper } = mountOverlay()
    await flushPromises()
    await nextTick()

    // A completion sheet with a typed note, then an unsaved editor edit: the
    // editor's save is the thing that will be refused.
    await wrapper.get('[data-status="done"]').trigger('click')
    await flushPromises()
    await nextTick()
    await wrapper.get('#task-resolution-text').setValue('Checked the runbook end to end.')
    await wrapper.get('#task-detail-name').setValue('Renamed while the sheet was open')
    await nextTick()

    expect(await requestClose(wrapper)).toBe(false)

    // The note belongs to a sheet above an editor that did not close; resetting
    // it before the refused save is how it used to disappear.
    expect(wrapper.find('.task-resolution-sheet').exists()).toBe(true)
    expect(wrapper.get<HTMLTextAreaElement>('#task-resolution-text').element.value)
      .toBe('Checked the runbook end to end.')
    expect(editor(wrapper).exists()).toBe(true)
    expect(editor(wrapper).get<HTMLTextAreaElement>('#task-detail-name').element.value)
      .toBe('Renamed while the sheet was open')
    expect(editor(wrapper).text()).toContain('that task changed on disk')
    expect(wrapper.emitted('close')).toBeUndefined()
    wrapper.unmount()
  })

  it('refuses a close when the title is blanked while the save is in flight', async () => {
    answerWith(BOARD)
    let resolvePatch: (value: unknown) => void = () => {}
    apiPatch.mockImplementationOnce(() => new Promise((resolve) => { resolvePatch = resolve }))
    const { wrapper } = mountOverlay()
    await flushPromises()
    await nextTick()

    await wrapper.get('#task-detail-name').setValue('Renamed')
    await nextTick()
    const closing = requestClose(wrapper)
    await flushPromises()
    expect(apiPatch).toHaveBeenCalledTimes(1)

    // The snapshot was taken before this edit; the close must not unmount it.
    await wrapper.get('#task-detail-name').setValue('   ')
    await nextTick()
    resolvePatch({
      workspace: 'personal',
      task: { ...task({ title: 'Renamed' }), revision: THIRD_REVISION, body: '' },
    })

    expect(await closing).toBe(false)
    await flushPromises()
    await nextTick()
    expect(editor(wrapper).exists()).toBe(true)
    expect(editor(wrapper).get<HTMLTextAreaElement>('#task-detail-name').element.value).toBe('   ')
    expect(editor(wrapper).text()).toContain('Add a title before closing')
    expect(wrapper.emitted('close')).toBeUndefined()
    wrapper.unmount()
  })

  it('writes title and body edits made while the close is saving, then closes', async () => {
    answerWith(BOARD)
    let resolvePatch: (value: unknown) => void = () => {}
    apiPatch.mockImplementationOnce(() => new Promise((resolve) => { resolvePatch = resolve }))
    apiPatch.mockResolvedValue({
      workspace: 'personal',
      task: { ...task({ title: 'Renamed twice' }), revision: FOURTH_REVISION, body: 'More prose.' },
    })
    const { wrapper } = mountOverlay()
    await flushPromises()
    await nextTick()

    await wrapper.get('#task-detail-name').setValue('Renamed')
    await nextTick()
    const closing = requestClose(wrapper)
    await flushPromises()
    expect(apiPatch).toHaveBeenCalledTimes(1)

    // Typed after the first snapshot, while that PATCH is unanswered.
    await wrapper.get('#task-detail-name').setValue('Renamed twice')
    await wrapper.get('#task-detail-body').setValue('More prose.')
    await nextTick()
    resolvePatch({
      workspace: 'personal',
      task: { ...task({ title: 'Renamed' }), revision: THIRD_REVISION, body: '' },
    })

    expect(await closing).toBe(true)
    await flushPromises()
    await nextTick()
    // The later edits are not unmounted with the dialog: they are written at
    // the revision the first answer returned, and only then does it close.
    expect(apiPatch).toHaveBeenCalledTimes(2)
    expect(apiPatch.mock.calls[1]![1]).toMatchObject({
      title: 'Renamed twice',
      body: 'More prose.',
      expected_revision: THIRD_REVISION,
    })
    expect(editor(wrapper).exists()).toBe(false)
    expect(wrapper.emitted('close')).toHaveLength(1)
    wrapper.unmount()
  })

  it('serializes rapid replacements and opens the latest task without losing the draft', async () => {
    answerWith(BOARD)
    let resolvePatch: (value: unknown) => void = () => {}
    apiPatch.mockImplementationOnce(() => new Promise((resolve) => { resolvePatch = resolve }))
    const { wrapper } = mountOverlay()
    await flushPromises()
    await nextTick()

    await wrapper.get('#task-detail-name').setValue('Renamed ship')
    await nextTick()
    await wrapper.setProps({ taskId: 'doing' })
    await flushPromises()
    expect(apiPatch).toHaveBeenCalledTimes(1)

    // A second replacement arrives while the first is still saving the editor.
    await wrapper.setProps({ taskId: 'held' })
    await flushPromises()
    expect(editor(wrapper).get<HTMLTextAreaElement>('#task-detail-name').element.value).toBe('Renamed ship')

    resolvePatch({
      workspace: 'personal',
      task: { ...task({ title: 'Renamed ship' }), revision: THIRD_REVISION, body: '' },
    })
    await flushPromises()
    await nextTick()
    await flushPromises()

    // The close finished and the serialized pass read the host's latest id, so
    // the pending draft was written and `held` is on screen rather than nothing.
    expect(apiPatch).toHaveBeenCalledTimes(1)
    expect(apiPatch.mock.calls[0]![1]).toMatchObject({
      title: 'Renamed ship',
      expected_revision: NEXT_REVISION,
    })
    expect(wrapper.get<HTMLTextAreaElement>('#task-detail-name').element.value).toBe('Wait on a key')
    expect(wrapper.emitted('detail-close')).toHaveLength(1)
    expect(wrapper.emitted('close')).toBeUndefined()
    wrapper.unmount()
  })

  it('does not send externally changed title and due date when saving a local description', async () => {
    answerWith(BOARD, 'Old prose.')
    const { wrapper } = mountOverlay()
    await flushPromises()
    await (await editBody(wrapper)).setValue('My draft.')
    const changed = task({ title: 'Their title', due: '2027-01-01', revision: THIRD_REVISION })
    apiGet.mockImplementation((url: string) => Promise.resolve(url.startsWith('/api/tasks?')
      ? { workspace: 'personal', tasks: [changed, ...BOARD.slice(1)] }
      : { workspace: 'personal', task: { ...changed, body: 'Old prose.' } }))
    const signals = useTaskSignalsStore()
    signals.loadedWorkspace = 'personal'
    signals.tasks = [changed]
    await flushPromises()
    apiPatch.mockResolvedValue({ workspace: 'personal', task: { ...changed, revision: FOURTH_REVISION, body: 'My draft.' } })
    expect(await requestClose(wrapper)).toBe(true)
    expect(apiPatch).toHaveBeenCalledTimes(1)
    expect(apiPatch.mock.calls[0]![1]).toMatchObject({ body: 'My draft.', expected_revision: THIRD_REVISION })
    expect(apiPatch.mock.calls[0]![1]).not.toHaveProperty('title')
    expect(apiPatch.mock.calls[0]![1]).not.toHaveProperty('due')
    wrapper.unmount()
  })

  it('reconciles a body draft when the row revision advances, and closes without losing it', async () => {
    answerWith(BOARD, 'Old prose.')
    const { wrapper } = mountOverlay()
    await flushPromises()
    await nextTick()

    const field = await editBody(wrapper)
    await field.setValue('My draft.')
    await nextTick()

    await advanceRowRevision(THIRD_REVISION, 'Old prose.')
    // The board dropped its description slot with the old revision, but the
    // editor's own base still knows this text is a draft, so it stays on screen.
    expect(wrapper.get<HTMLTextAreaElement>('#task-detail-body').element.value).toBe('My draft.')

    apiPatch.mockResolvedValue({
      workspace: 'personal',
      task: { ...task(), revision: FOURTH_REVISION, body: 'My draft.' },
    })
    expect(await requestClose(wrapper)).toBe(true)
    await flushPromises()
    await nextTick()

    // The fresh read showed the same prose, so the draft is rebased onto its
    // revision and written there — not dropped, and not written at the stale one.
    expect(apiPatch).toHaveBeenCalledTimes(1)
    expect(apiPatch.mock.calls[0]![1]).toMatchObject({
      expected_revision: THIRD_REVISION,
      body: 'My draft.',
    })
    expect(editor(wrapper).exists()).toBe(false)
    expect(wrapper.emitted('close')).toHaveLength(1)
    wrapper.unmount()
  })

  it('refuses to close or replace when the description changed under a body draft', async () => {
    answerWith(BOARD, 'Old prose.')
    const { wrapper } = mountOverlay()
    await flushPromises()
    await nextTick()

    const field = await editBody(wrapper)
    await field.setValue('My draft.')
    await nextTick()
    await advanceRowRevision(THIRD_REVISION, 'Their prose.')

    expect(await requestClose(wrapper)).toBe(false)
    expect(apiPatch).not.toHaveBeenCalled()
    expect(wrapper.get<HTMLTextAreaElement>('#task-detail-body').element.value).toBe('My draft.')
    expect(editor(wrapper).text()).toContain('description changed on disk')
    expect(wrapper.emitted('close')).toBeUndefined()

    // A host replacement performs the same internal close and must refuse the
    // same way, keeping both the old task and the text.
    await wrapper.setProps({ taskId: 'doing' })
    await flushPromises()
    await nextTick()
    expect(wrapper.get<HTMLTextAreaElement>('#task-detail-name').element.value).toBe('Ship the board')
    expect(wrapper.get<HTMLTextAreaElement>('#task-detail-body').element.value).toBe('My draft.')
    expect(wrapper.emitted('detail-close')).toBeUndefined()
    expect(wrapper.emitted('close')).toBeUndefined()

    // The conflict has an explicit way out that keeps the text: choosing it
    // rebases the draft and writes it at the revision on disk.
    apiPatch.mockResolvedValue({
      workspace: 'personal',
      task: { ...task(), revision: FOURTH_REVISION, body: 'My draft.' },
    })
    const overwrite = editor(wrapper).findAll('button')
      .find((button) => button.text() === 'Overwrite with my text')!
    await overwrite.trigger('click')
    await flushPromises()
    await nextTick()
    expect(apiPatch).toHaveBeenCalledWith('/api/tasks/ship', expect.objectContaining({
      expected_revision: THIRD_REVISION,
      body: 'My draft.',
    }))
    // With the conflict resolved and the draft stored, the host's deferred
    // replacement can finally open its task.
    expect(wrapper.get<HTMLTextAreaElement>('#task-detail-name').element.value).toBe('Wire the store')
    expect(wrapper.emitted('detail-close')).toHaveLength(1)
    expect(editor(wrapper).text()).not.toContain('description changed on disk')
    wrapper.unmount()
  })

  it('refuses to close when a save reports success but leaves the body draft unwritten', async () => {
    answerWith(BOARD, 'Old prose.')
    const { wrapper } = mountOverlay()
    await flushPromises()
    await nextTick()

    const field = await editBody(wrapper)
    await field.setValue('My draft.')
    await nextTick()
    await advanceRowRevision(THIRD_REVISION, 'Old prose.')

    // A 200 whose record no longer carries the text: the draft did not move
    // during the write, so an unchanged form is not proof it was stored.
    apiPatch.mockResolvedValue({
      workspace: 'personal',
      task: { ...task(), revision: THIRD_REVISION, body: '' },
    })
    expect(await requestClose(wrapper)).toBe(false)

    expect(apiPatch).toHaveBeenCalledTimes(1)
    expect(wrapper.get<HTMLTextAreaElement>('#task-detail-body').element.value).toBe('My draft.')
    expect(editor(wrapper).text()).toContain('could not be saved completely')
    expect(editor(wrapper).exists()).toBe(true)
    expect(wrapper.emitted('close')).toBeUndefined()
    wrapper.unmount()
  })

  it('keeps and copies a dirty draft when the open task disappears from the board', async () => {
    answerWith(BOARD, 'Old prose.')
    const { wrapper } = mountOverlay()
    await flushPromises()
    await nextTick()

    await wrapper.get('#task-detail-name').setValue('My renamed task')
    const field = await editBody(wrapper)
    await field.setValue('My draft.')
    await nextTick()

    // The task is deleted elsewhere; the board's refresh no longer carries it.
    apiGet.mockImplementation((url: string) => (
      url.startsWith('/api/tasks?')
        ? Promise.resolve({
          workspace: 'personal',
          tasks: BOARD.filter((row) => row.id !== 'ship'),
        })
        : Promise.resolve({ workspace: 'personal', task: { ...task({ id: 'doing' }), revision: NEXT_REVISION, body: '' } })
    ))
    const signals = useTaskSignalsStore()
    signals.loadedWorkspace = 'personal'
    signals.tasks = []
    await flushPromises()
    await nextTick()

    // A missing row is not "nothing unsaved": the editor and both drafts stay.
    expect(editor(wrapper).exists()).toBe(true)
    expect(wrapper.get<HTMLTextAreaElement>('#task-detail-name').element.value).toBe('My renamed task')
    expect(wrapper.get<HTMLTextAreaElement>('#task-detail-body').element.value).toBe('My draft.')

    apiPatch.mockClear()
    expect(await requestClose(wrapper)).toBe(false)
    expect(editor(wrapper).text()).toContain('removed from the board')
    expect(apiPatch).not.toHaveBeenCalled()
    expect(apiPost).not.toHaveBeenCalled()

    // The escape hatch: the draft is copyable as plain text, with a spoken result.
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
    const copy = editor(wrapper).findAll('button')
      .find((button) => button.text() === 'Copy draft')!
    await copy.trigger('click')
    await flushPromises()
    expect(writeText).toHaveBeenCalledTimes(1)
    const copied = writeText.mock.calls[0]![0] as string
    expect(copied).toContain('My renamed task')
    expect(copied).toContain('My draft.')
    expect(editor(wrapper).text()).toContain('Draft copied.')

    // A replacement is refused for the same reason and keeps the old editor.
    await wrapper.setProps({ taskId: 'doing' })
    await flushPromises()
    await nextTick()
    expect(wrapper.get<HTMLTextAreaElement>('#task-detail-name').element.value).toBe('My renamed task')
    expect(wrapper.get<HTMLTextAreaElement>('#task-detail-body').element.value).toBe('My draft.')
    expect(wrapper.emitted('detail-close')).toBeUndefined()
    expect(wrapper.emitted('close')).toBeUndefined()

    // Nothing to lose: with the body returned to its saved text (the title was
    // already saved before the row vanished), a close is safe and allowed.
    await (await editBody(wrapper)).setValue('Old prose.')
    await nextTick()
    expect(wrapper.get<HTMLTextAreaElement>('#task-detail-name').element.value).toBe('My renamed task')
    expect(await requestClose(wrapper)).toBe(true)
    expect(wrapper.emitted('close')).toHaveLength(1)
    wrapper.unmount()
  })

  it.each(['close', 'replacement'])('confirms discarding a deleted task draft for %s', async (action) => {
    answerWith(BOARD, 'Old prose.')
    const { wrapper } = mountOverlay()
    await flushPromises()
    await (await editBody(wrapper)).setValue('Keep me until confirmed.')
    answerWith(BOARD.filter((row) => row.id !== 'ship'), '')
    const signals = useTaskSignalsStore()
    signals.loadedWorkspace = 'personal'
    signals.tasks = []
    await flushPromises()
    if (action === 'replacement') {
      await wrapper.setProps({ taskId: 'doing' })
      await flushPromises()
    } else {
      expect(await requestClose(wrapper)).toBe(false)
    }
    askConfirm.mockResolvedValueOnce(false).mockResolvedValueOnce(true)
    const discard = () => editor(wrapper).findAll('button').find((button) => button.text() === 'Discard draft')!
    expect(discard()).toBeDefined()
    await discard().trigger('click')
    await flushPromises()
    expect(wrapper.get<HTMLTextAreaElement>('#task-detail-body').element.value).toBe('Keep me until confirmed.')
    await discard().trigger('click')
    await flushPromises()
    await nextTick()
    expect(apiPatch).not.toHaveBeenCalled()
    if (action === 'replacement') {
      expect(wrapper.get<HTMLTextAreaElement>('#task-detail-name').element.value).toBe('Wire the store')
    } else {
      expect(wrapper.emitted('close')).toHaveLength(1)
    }
    wrapper.unmount()
  })

  it('offers discard for a deleted task even when its title is blank', async () => {
    answerWith(BOARD, 'Old prose.')
    const { wrapper } = mountOverlay()
    await flushPromises()
    await wrapper.get('#task-detail-name').setValue('')
    answerWith(BOARD.filter((row) => row.id !== 'ship'))
    const signals = useTaskSignalsStore()
    signals.loadedWorkspace = 'personal'
    signals.tasks = []
    await flushPromises()
    expect(await requestClose(wrapper)).toBe(false)
    expect(editor(wrapper).text()).toContain('Discard draft')
    wrapper.unmount()
  })
})
