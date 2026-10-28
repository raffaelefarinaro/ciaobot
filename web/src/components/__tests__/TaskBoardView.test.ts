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
import { DOMWrapper, flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import TaskBoardView from '../TaskBoardView.vue'
import { useProjectStore } from '../../stores/projects'
import { useTaskBoardStore } from '../../stores/taskBoard'
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

// The app's confirm lives in App.vue, not in the pane, so it is mocked rather
// than mounted: a delete has to be answerable from here without a second dialog.
// `pendingConfirm` is a real ref-shaped slot because the pane reads it to decide
// whether its own Escape handling applies while a confirm is up.
vi.mock('../../lib/confirm', () => ({
  askConfirm,
  askConfirmReason,
  pendingConfirm,
}))

// A due date as the runner's own locale shows it: "Mar 20" in en-US, "20 Mar"
// in en-GB or en-CH.
function shortDate(year: number, month: number, day: number): string {
  return new Date(year, month - 1, day).toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
}

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
    // A done fixture is finished today, as the board's own fixtures say; a test that
    // needs an undated done task passes `completed_at: null` explicitly.
    completed_at: overrides.status === 'done' ? '2026-03-01T08:00:00Z' : null,
    has_resolution: false,
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
 * The description's editor. A described task opens rendered, so this clicks
 * the rendered body first when the textarea is not already up.
 */
async function bodyField(wrapper: ReturnType<typeof mount>) {
  if (!wrapper.find('#task-detail-body').exists()) {
    await wrapper.get('.task-body').trigger('click')
    await nextTick()
  }
  return wrapper.get<HTMLTextAreaElement>('#task-detail-body')
}

// ── Pointer drags ─────────────────────────────────────────────────────────
//
// jsdom has no layout, so the board's geometry is laid out from the DOM on every
// read: lanes 320px apart, cards 40px tall with an 8px gap under a 40px heading,
// the picked-up card collapsed to nothing, the Done tab after the last lane.

const CARD_HEIGHT = 40
const CARD_GAP = 8
const LANE_HEAD = 40
const LANE_STEP = 320

function rectOf(x: number, y: number, width: number, height: number): DOMRect {
  return new DOMRect(x, y, width, height)
}

function laneIndexOf(lane: Element): number {
  return [...document.querySelectorAll('.task-lane')].indexOf(lane)
}

function layOutBoard() {
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
    if (this.matches('.task-lane')) return rectOf(laneIndexOf(this) * LANE_STEP, 0, 310, 10000)
    if (this.matches('.task-done-toggle')) {
      return rectOf(document.querySelectorAll('.task-lane').length * LANE_STEP, 0, 44, 300)
    }
    if (this.matches('.task-cards > li')) {
      const lane = this.closest('.task-lane')!
      let top = LANE_HEAD
      for (const sibling of [...this.parentElement!.children]) {
        if (sibling === this) break
        top += (sibling.classList.contains('drag-source') ? 0 : CARD_HEIGHT) + CARD_GAP
      }
      return rectOf(laneIndexOf(lane) * LANE_STEP, top, 310, this.classList.contains('drag-source') ? 0 : CARD_HEIGHT)
    }
    return rectOf(0, 0, 0, 0)
  })
  document.elementsFromPoint = (x, y) => {
    const hits: Element[] = []
    for (const el of document.querySelectorAll('.task-lane, .task-done-toggle')) {
      const rect = el.getBoundingClientRect()
      if (x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom) hits.push(el)
    }
    return hits
  }
  Element.prototype.setPointerCapture = vi.fn()
  Element.prototype.releasePointerCapture = vi.fn()
  Element.prototype.hasPointerCapture = vi.fn(() => true)
  Element.prototype.scrollIntoView = vi.fn()
  vi.stubGlobal('requestAnimationFrame', () => 1)
  vi.stubGlobal('cancelAnimationFrame', () => undefined)
}

function pointer(type: string, x: number, y: number): PointerEvent {
  return new PointerEvent(type, {
    clientX: x, clientY: y, pointerId: 1, isPrimary: true, button: 0, bubbles: true, cancelable: true,
  })
}

/** Where the pointer must be to land at `index` in lane `laneIndex` (the lane's x, a y between its cards). */
function dropPoint(laneIndex: number, index: number) {
  return { x: laneIndex * LANE_STEP + 100, y: LANE_HEAD + index * (CARD_HEIGHT + CARD_GAP) }
}

const boardRoot = () => document.querySelector<HTMLElement>('.task-lanes')!

/** Press a card's grip and move far enough to pick it up. */
async function pickUp(wrapper: ReturnType<typeof mount>, title: string) {
  layOutBoard()
  const grip = card(wrapper, title).get('.drag-grip').element as HTMLElement
  const at = card(wrapper, title).element.getBoundingClientRect()
  grip.dispatchEvent(pointer('pointerdown', at.left + 10, at.top + 10))
  boardRoot().dispatchEvent(pointer('pointermove', at.left + 10, at.top + 24))
  await nextTick()
}

async function dragTo(x: number, y: number) {
  boardRoot().dispatchEvent(pointer('pointermove', x, y))
  await nextTick()
}

async function release(x: number, y: number) {
  boardRoot().dispatchEvent(pointer('pointerup', x, y))
  await flushPromises()
  await nextTick()
}

/** Drag a card to `index` in one of the lanes with the pointer, and release. */
async function dragCardTo(wrapper: ReturnType<typeof mount>, title: string, laneIndex: number, index: number) {
  await pickUp(wrapper, title)
  const at = dropPoint(laneIndex, index)
  await dragTo(at.x, at.y)
  await release(at.x, at.y)
}

/** Drag a card to the end of one of the four columns, as the pointer does. */
async function dragCard(wrapper: ReturnType<typeof mount>, title: string, laneIndex: number) {
  await dragCardTo(wrapper, title, laneIndex, 99)
}

const liveText = (wrapper: ReturnType<typeof mount>) => wrapper.get('[role="status"][aria-live="polite"]').text()
const cardTitles = (wrapper: ReturnType<typeof mount>, laneIndex: number) =>
  lanes(wrapper)[laneIndex]!.findAll('.task-card:not(.drag-source) .task-open').map((el) => el.text())

describe('TaskBoardView', () => {
  /** Reports the pane's inline size, which is what `NARROW_PANE_PX` reads. */
  let reportPaneWidth = (_width: number) => {}

  beforeEach(() => {
    // The fixtures are dated 2026-03-01; Done keeps only today's, so the
    // board is read on that day.
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-03-01T12:00:00Z'))
    // Done is collapsed by default; most of these tests read the whole board.
    localStorage.setItem('ciao.tasks.doneShown', '1')
    setActivePinia(createPinia())
    apiGet.mockReset()
    apiPost.mockReset()
    apiPatch.mockReset()
    apiDel.mockReset()
    askConfirm.mockReset()
    askConfirmReason.mockReset()
    askConfirmReason.mockResolvedValue({ confirmed: true, reason: '' })
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
    vi.useRealTimers()
    document.body.innerHTML = ''
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('re-reads when the app-wide task signals refresh for this workspace', async () => {
    await mountBoard()
    const boardReads = () => apiGet.mock.calls.filter((c) => String(c[0]).startsWith('/api/tasks?')).length
    const before = boardReads()
    const signals = useTaskSignalsStore()

    // Another workspace's refresh is not this board's news.
    signals.loadedWorkspace = 'work'
    signals.tasks = []
    await flushPromises()
    expect(boardReads()).toBe(before)

    signals.loadedWorkspace = 'personal'
    signals.tasks = [...BOARD] as Task[]
    await flushPromises()
    expect(boardReads()).toBe(before + 1)
  })

  it('keeps Done to today by completion time, with the rest one click away', async () => {
    const wrapper = await mountBoard([
      task({ id: 'today', title: 'Landed today', status: 'done', completed_at: '2026-03-01T08:00:00Z' }),
      // Completed last week, edited today: the edit does not make it today's.
      task({
        id: 'old', title: 'Landed last week', status: 'done',
        completed_at: '2026-02-20T09:00:00+00:00', updated_at: '2026-03-01T09:00:00+00:00',
      }),
      // No completion stamp (completed before records existed): not today's, so it
      // sits behind Show all with the earlier one.
      task({ id: 'untimed', title: 'Done without a stamp', status: 'done', completed_at: null }),
    ])
    const done = lanes(wrapper).find((lane) => lane.get('.task-lane-label').text() === 'Done')!
    expect(done.text()).toContain('Landed today')
    expect(done.text()).not.toContain('Done without a stamp')
    expect(done.text()).not.toContain('Landed last week')
    const more = done.get('.task-lane-more')
    expect(more.text()).toBe('2 done earlier · Show all')

    await more.trigger('click')
    await nextTick()
    expect(wrapper.text()).toContain('Landed last week')
    expect(wrapper.text()).toContain('Done without a stamp')
    expect(wrapper.find('.task-lane-more').exists()).toBe(false)
  })

  it('keeps Done behind a tab by default, and remembers the choice in localStorage', async () => {
    localStorage.removeItem('ciao.tasks.doneShown')
    const wrapper = await mountBoard()
    expect(lanes(wrapper).map((lane) => lane.get('.task-lane-label').text()))
      .toEqual(['To do', 'In progress', 'In review'])
    const tab = wrapper.get('.task-done-toggle')
    expect(tab.attributes('aria-expanded')).toBe('false')
    expect(tab.attributes('aria-label')).toBe('Show done tasks (1)')  // no earlier ones: no earlier clause
    expect(tab.text()).toContain('Done')

    await tab.trigger('click')
    await flushPromises()
    expect(localStorage.getItem('ciao.tasks.doneShown')).toBe('1')
    expect(lanes(wrapper)).toHaveLength(4)
    expect(wrapper.find('.task-done-toggle').exists()).toBe(false)
    const hide = wrapper.get('.task-lane-hide')
    expect(hide.attributes('aria-label')).toBe('Hide done tasks')
    expect(hide.attributes('aria-expanded')).toBeUndefined()
    expect(hide.text()).toBe('Hide')
    await hide.trigger('click')
    expect(localStorage.getItem('ciao.tasks.doneShown')).toBe('0')
    expect(lanes(wrapper)).toHaveLength(3)
    wrapper.unmount()

    // A fresh mount reads the stored choice back.
    localStorage.setItem('ciao.tasks.doneShown', '1')
    const again = await mountBoard()
    expect(lanes(again)).toHaveLength(4)
    expect(again.find('.task-done-toggle').exists()).toBe(false)
    again.unmount()
  })

  it('moves focus to the Hide button when the Done tab opens the lane, since the tab unmounts', async () => {
    localStorage.removeItem('ciao.tasks.doneShown')
    const wrapper = await mountBoard()
    await wrapper.get('.task-done-toggle').trigger('click')
    await flushPromises()
    await nextTick()
    expect(document.activeElement).toBe(wrapper.get('.task-lane-hide').element)
    expect(wrapper.get('[data-lane="done"]').attributes('aria-label')).toBe('Done')
    wrapper.unmount()
  })

  it('counts today in the tab and names earlier done tasks only when there are some', async () => {
    localStorage.removeItem('ciao.tasks.doneShown')
    const earlier = task({ id: 'old', title: 'Long ago', status: 'done', completed_at: '2026-02-01T08:00:00Z' })
    const wrapper = await mountBoard([...BOARD, earlier])
    expect(wrapper.get('.task-done-toggle').attributes('aria-label')).toBe('Show done tasks (1 today, 1 earlier)')
    expect(wrapper.get('.task-done-toggle .badge').text()).toBe('1')
    wrapper.unmount()
  })

  it('keeps focus on a real element when Shift+Right completes a card into the collapsed Done', async () => {
    localStorage.removeItem('ciao.tasks.doneShown')
    const sibling = task({ id: 'held2', title: 'Wait on a second key', status: 'in_review' })
    const wrapper = await mountBoard([...BOARD, sibling])
    apiPost.mockResolvedValue({ workspace: 'personal', task: { ...task({ id: 'held', status: 'done' }), revision: NEXT_REVISION, body: '' } })
    const open = card(wrapper, 'Wait on a key').get('.task-open')
    ;(open.element as HTMLElement).focus()
    await open.trigger('keydown', { key: 'ArrowRight', shiftKey: true })
    await flushPromises()
    await wrapper.get('.task-resolution-sheet form').trigger('submit')
    await flushPromises()
    await nextTick()
    // Done is collapsed, so the card left the view: focus goes to its lane neighbour.
    expect(document.activeElement).toBe(card(wrapper, 'Wait on a second key').get('.task-open').element)
    // With nothing left in the lane it goes to the Done tab.
    const last = card(wrapper, 'Wait on a second key').get('.task-open')
    ;(last.element as HTMLElement).focus()
    apiPost.mockResolvedValue({ workspace: 'personal', task: { ...task({ id: 'held2', status: 'done' }), revision: NEXT_REVISION, body: '' } })
    await last.trigger('keydown', { key: 'ArrowRight', shiftKey: true })
    await flushPromises()
    await wrapper.get('.task-resolution-sheet form').trigger('submit')
    await flushPromises()
    await nextTick()
    expect(document.activeElement).toBe(wrapper.get('.task-done-toggle').element)
    wrapper.unmount()
  })

  it('completes a card dropped on the collapsed Done tab through the completion gesture', async () => {
    localStorage.removeItem('ciao.tasks.doneShown')
    const wrapper = await mountBoard()
    await pickUp(wrapper, 'Wait on a key')
    const tab = wrapper.get('.task-done-toggle')
    await dragTo(lanes(wrapper).length * LANE_STEP + 10, 50)
    expect(tab.classes()).toContain('task-done-toggle--drop')
    await release(lanes(wrapper).length * LANE_STEP + 10, 50)
    expect(wrapper.find('[role="dialog"]').exists()).toBe(true)
    wrapper.unmount()
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
    expect(shown.get('.task-meta').text()).toBe(`Website${shortDate(2099, 3, 20)}`)
    expect(shown.get('.task-due').text()).toBe(shortDate(2099, 3, 20))
    // Who it is for is the Agent block's business, not a label on every card.
    expect(shown.text()).not.toContain('For the agent')
    // Review is the column a card sits in (#1069), not a badge on it.
    expect(shown.text()).not.toContain('Review')
    wrapper.unmount()
  })

  it('marks a past due date overdue in words as well as in colour', async () => {
    const wrapper = await mountBoard([task({ due: '2000-01-01' })])
    expect(card(wrapper, 'Ship the board').text()).toContain('Overdue')
    expect(card(wrapper, 'Ship the board').find('.task-due--over').exists()).toBe(true)
    wrapper.unmount()
  })

  it('does not mark a done task overdue', async () => {
    const wrapper = await mountBoard([task({ status: 'done', due: '2000-01-01' })])
    const shown = card(wrapper, 'Ship the board')
    expect(shown.text()).not.toContain('Overdue')
    expect(shown.find('.task-due--over').exists()).toBe(false)
    wrapper.unmount()
  })

  it('says nothing for the defaults: General, For me and no date leave no meta line', async () => {
    const wrapper = await mountBoard([task()])
    const shown = card(wrapper, 'Ship the board')
    expect(shown.find('.task-meta').exists()).toBe(false)
    // And an undelegated card has no band: its Done is the checkbox, and its menu
    // holds only Move up / Move down.
    expect(shown.find('.task-agent-band').exists()).toBe(false)
    expect(shown.find('.task-more').exists()).toBe(true)
    expect(shown.find('select').exists()).toBe(false)
    wrapper.unmount()
  })

  it('names each lane in its heading, without repeating status on cards, at a narrow pane', async () => {
    const wrapper = await mountBoard()
    reportPaneWidth(390)
    await nextTick()
    expect(lanes(wrapper)).toHaveLength(4)
    // The four lanes share one row; the board scrolls them sideways.
    expect(wrapper.findAll('.task-lanes')).toHaveLength(1)
    expect(wrapper.get('.task-lanes').classes()).toContain('task-lanes--columns')
    const review = lanes(wrapper).find(lane => lane.get('.task-lane-label').text() === 'In review')!
    expect(review.text()).toContain('Wait on a key')
    expect(card(wrapper, 'Wait on a key').find('.task-status-badge').exists()).toBe(false)
    // The lanes stay side by side at every width, so the grip holds at a narrow pane.
    expect(card(wrapper, 'Wait on a key').get('.drag-grip').attributes('aria-label')).toBe('Reorder Wait on a key')
    expect(card(wrapper, 'Wait on a key').attributes('draggable')).toBeUndefined()
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
    expect(apiPost).not.toHaveBeenCalled()
    expect(wrapper.get('.task-resolution-sheet').text()).toContain('Complete with resolution')
    await wrapper.get('.task-resolution-sheet form').trigger('submit')
    await flushPromises()
    // Dropped at the end of a Done lane that holds one card: the placement is
    // held until the sheet is confirmed, then rides on the completion.
    expect(apiPost).toHaveBeenCalledWith('/api/tasks/ship/complete', {
      workspace: 'personal',
      expected_revision: REVISION,
      resolution: '',
      placement: { before_id: null, after_id: 'done', expected_order_version: 0 },
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
    await wrapper.get('.task-resolution-sheet form').trigger('submit')
    await flushPromises()
    await nextTick()

    expect(apiPost).toHaveBeenCalledWith('/api/tasks/ship/complete', {
      workspace: 'personal',
      expected_revision: REVISION,
      resolution: '',
    })
    expect(lanes(wrapper)[3]!.text()).toContain('Ship the board')
    wrapper.unmount()
  })

  it('surfaces a 409 with the server\'s sentence and keeps the row where it was', async () => {
    const wrapper = await mountBoard()
    apiPatch.mockRejectedValue(Object.assign(new Error('HTTP 409'), {
      payload: { error: { code: 'task_revision_conflict', message: 'that task changed on disk', retryable: true } },
    }))

    // One lane over by keyboard: a status write with no placement, so the store
    // shows the server's own sentence (a placed drag's 409 says the board changed).
    await card(wrapper, 'Ship the board').get('.task-open').trigger('keydown', { key: 'ArrowRight', shiftKey: true })
    await flushPromises()
    await nextTick()

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

  it('lists linked chats and unlinks one', async () => {
    useProjectStore().chats = [
      { chat_id: 'c1', project_id: 'p1', title: 'Design review', archived: false } as never,
    ]
    const linked = task({ linked_chats: ['c1'] })
    apiGet.mockImplementation((url: string) => Promise.resolve(
      url.includes('/api/tasks?')
        ? { workspace: 'personal', tasks: [linked] }
        : detailAnswer(linked, 'Prose the list never carried.'),
    ))
    apiPost.mockResolvedValue({
      workspace: 'personal',
      task: { ...linked, linked_chats: [], revision: THIRD_REVISION, body: '' },
    })
    const wrapper = mount(TaskBoardView, { attachTo: document.body })
    await flushPromises()
    await nextTick()

    await card(wrapper, 'Ship the board').get('.task-open').trigger('click')
    await flushPromises()
    await nextTick()

    const section = wrapper.get('.task-linked-chats')
    expect(section.text()).toContain('Linked chats')
    expect(section.text()).toContain('Design review')

    await section.get('[aria-label="Unlink chat Design review"]').trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith('/api/tasks/ship/chats', {
      workspace: 'personal',
      expected_revision: NEXT_REVISION,
      chat_id: 'c1',
      remove: true,
    })
    expect(wrapper.find('.task-linked-chats').exists()).toBe(false)
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

  it('edits the description in place: a click opens it, a link still opens, blur renders it again', async () => {
    const wrapper = await mountBoard()
    apiGet.mockResolvedValue(detailAnswer(task(), 'See [the spec](https://example.com/spec).\n'))
    apiPatch.mockResolvedValue({ workspace: 'personal', task: { ...task(), revision: THIRD_REVISION, body: '' } })
    await card(wrapper, 'Ship the board').get('.task-open').trigger('click')
    await flushPromises()
    await nextTick()
    // No separate Edit control: the rendered body is the way in.
    expect(wrapper.findAll('.task-detail-main button').some((b) => b.text() === 'Edit')).toBe(false)
    await wrapper.get('.task-body a').trigger('click')
    expect(wrapper.find('#task-detail-body').exists()).toBe(false)
    await wrapper.get('.task-body').trigger('click')
    await nextTick()
    const field = wrapper.get<HTMLTextAreaElement>('#task-detail-body')
    await field.setValue('See the spec, updated.\n')
    await field.trigger('blur')
    await flushPromises()
    expect(wrapper.find('#task-detail-body').exists()).toBe(false)
    expect(wrapper.get('.task-body').text()).toBe('See the spec, updated.')
    wrapper.unmount()
  })

  it('draws progress notes as bubbles with their links live, and names the agent\'s', async () => {
    const wrapper = await mountBoard()
    const note = (id: string, actor: string, text: string) => ({
      id, actor, text, recorded_at: '2026-10-14T09:30:00Z', edited_at: null, attempt_id: '', chat_id: '',
    })
    const read = detailAnswer(task(), 'Ship it.\n')
    apiGet.mockResolvedValue({ ...read, task: { ...read.task, updates: [
      note('u2', 'agent', 'Blocked on auth.'),
      note('u1', 'user', 'Asked about it https://example.slack.com/archives/C1/p2'),
    ] } })
    await card(wrapper, 'Ship the board').get('.task-open').trigger('click')
    await flushPromises()
    await nextTick()
    const notes = wrapper.findAll('.task-note')
    expect(notes.map((n) => n.attributes('data-actor'))).toEqual(['agent', 'user'])
    expect(notes[0]!.get('.task-note-author').text()).toBe('Agent')
    const link = notes[1]!.get('.task-note-text a')
    expect(link.attributes('href')).toBe('https://example.slack.com/archives/C1/p2')
    expect(link.attributes('target')).toBe('_blank')
    wrapper.unmount()
  })

  it('completes from Status alone: an open task carries no separate Complete action', async () => {
    const wrapper = await mountBoard()
    apiGet.mockResolvedValue(detailAnswer(task(), ''))
    await card(wrapper, 'Ship the board').get('.task-open').trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.find('.task-resolution').exists()).toBe(false)
    await wrapper.get('.task-status-opt[data-status="done"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('.task-resolution-sheet').text()).toContain('Complete')
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
    expect(apiPost).not.toHaveBeenCalled()
    expect(wrapper.get('[data-status="done"]').attributes('aria-checked')).toBe('false')
    await wrapper.get('.task-resolution-sheet form').trigger('submit')
    await flushPromises()
    await nextTick()
    expect(apiPost).toHaveBeenCalledWith('/api/tasks/ship/complete', {
      workspace: 'personal',
      expected_revision: NEXT_REVISION,
      resolution: '',
    })
    expect(wrapper.find('#task-detail-name').exists()).toBe(false)
    expect(card(wrapper, 'Ship the board').classes()).toContain('task-card--done')
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
    askConfirmReason.mockImplementation((message = '', options = {}) => {
      // The real module parks the outstanding request here for the duration.
      pendingConfirm.value = {
        message,
        title: options.title ?? 'Are you sure?',
        destructive: true,
        resolve: () => {},
      }
      return new Promise<{ confirmed: boolean, reason: string }>((resolve) => {
        release = (ok: boolean) => {
          pendingConfirm.value = null
          resolve({ confirmed: ok, reason: '' })
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

  it('does not render Reload on a healthy board', async () => {
    const wrapper = await mountBoard()
    expect(wrapper.get('.task-lede').text()).toContain('3 open of 4')
    expect(wrapper.findAll('button').some((b) => b.text() === 'Reload')).toBe(false)
    wrapper.unmount()
  })

  /** Serve one task's detail read for the open dialog, and the board list for everything else. */
  function mockShipDetail() {
    apiGet.mockImplementation((url: string) => Promise.resolve(
      url.startsWith('/api/tasks/ship?')
        ? detailAnswer(task(), '')
        : url.includes('/api/tasks?') ? { workspace: 'personal', tasks: BOARD } : {},
    ))
  }

  /** Open the "Ship the board" dialog and its resolution sheet. */
  async function openResolutionSheet(wrapper: ReturnType<typeof mount>) {
    await card(wrapper, 'Ship the board').get('.task-open').trigger('click')
    await flushPromises()
    await nextTick()
    await wrapper.get('.task-status-opt[data-status="done"]').trigger('click')
    await flushPromises()
    await nextTick()
  }

  it('offers no resolution action on a legacy done task, and keeps it on one with a completion (#1189)', async () => {
    // A done task from before completion records has no completion: its row says
    // `completed_at: null` and its read has an empty history, so the action row is
    // left out. A done task with a completion keeps its Edit resolution action.
    const legacy = task({ id: 'legacy', title: 'Old finished work', status: 'done', completed_at: null })
    const timed = task({
      id: 'timed', title: 'Recent finished work', status: 'done',
      completed_at: '2026-03-01T08:00:00+00:00', has_resolution: true,
    })
    apiGet.mockImplementation((url: string) => {
      if (url.startsWith('/api/tasks/legacy?')) {
        return Promise.resolve({ workspace: 'personal', task: { ...legacy, revision: NEXT_REVISION, body: '', resolution: '', completions: [], delegation_log: '' } })
      }
      if (url.startsWith('/api/tasks/timed?')) {
        return Promise.resolve({ workspace: 'personal', task: { ...timed, revision: NEXT_REVISION, body: '', resolution: 'Checked it', completions: [{ id: 'a'.repeat(32), completed_at: '2026-03-01T08:00:00+00:00', resolution: 'Checked it', edited_at: null, attempt_id: '' }], delegation_log: '' } })
      }
      return Promise.resolve(url.includes('/api/tasks?') ? { workspace: 'personal', tasks: [legacy, timed] } : {})
    })
    const wrapper = mount(TaskBoardView, { attachTo: document.body })
    await flushPromises()
    await nextTick()
    // A done task without a completion stamp sits behind Show all.
    await wrapper.get('.task-lane-more').trigger('click')
    await nextTick()

    await card(wrapper, 'Old finished work').get('.task-open').trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.get('.task-resolution').text()).toContain('(no time recorded)')
    expect(wrapper.findAll('.task-resolution button').map((b) => b.text())).toEqual([])
    wrapper.unmount()

    const second = mount(TaskBoardView, { attachTo: document.body })
    await flushPromises()
    await nextTick()
    await card(second, 'Recent finished work').get('.task-open').trigger('click')
    await flushPromises()
    await nextTick()
    expect(second.get('.task-resolution').text()).not.toContain('(no time recorded)')
    expect(second.findAll('.task-resolution button').map((b) => b.text())).toContain('Edit resolution')
    second.unmount()
  })

  it('completes with a resolution in one request, and cancel does not post', async () => {
    const wrapper = await mountBoard()
    mockShipDetail()
    apiPost.mockResolvedValue({
      workspace: 'personal',
      task: { ...task({ status: 'done', completed_at: '2026-03-01T12:00:00+00:00', has_resolution: true }), revision: THIRD_REVISION, body: '', resolution: 'Checked it', completions: [], delegation_log: '' },
    })
    await openResolutionSheet(wrapper)

    // Cancel first: the sheet closes and nothing is sent.
    const sheet = () => wrapper.find('.task-resolution-sheet')
    expect(sheet().exists()).toBe(true)
    await sheet().findAll('button').find((b) => b.text() === 'Cancel')!.trigger('click')
    await nextTick()
    expect(sheet().exists()).toBe(false)
    expect(apiPost).not.toHaveBeenCalled()
    expect(wrapper.find('#task-detail-name').exists()).toBe(true)

    // Reopen, write the note, and submit: one POST carrying status and note.
    await openResolutionSheet(wrapper)
    await wrapper.get('#task-resolution-text').setValue('Checked the export by hand')
    await sheet().get('form').trigger('submit')
    await flushPromises()
    await nextTick()

    expect(apiPost).toHaveBeenCalledTimes(1)
    expect(apiPost).toHaveBeenCalledWith('/api/tasks/ship/complete', {
      workspace: 'personal',
      expected_revision: NEXT_REVISION,
      resolution: 'Checked the export by hand',
    })
    expect(apiPatch).not.toHaveBeenCalled()
    expect(sheet().exists()).toBe(false)
    expect(wrapper.find('#task-detail-name').exists()).toBe(false)
    wrapper.unmount()
  })

  it('offers one enabled completion action and Cancel, with no separate no-note button', async () => {
    const wrapper = await mountBoard()
    mockShipDetail()
    await openResolutionSheet(wrapper)

    const actions = wrapper.get('.task-resolution-sheet .form-actions')
    // One primary action and Cancel. The note is optional, so an empty draft
    // leaves the primary action available.
    expect(actions.findAll('button').map((b) => b.text())).toEqual(['Mark done', 'Cancel'])
    expect(actions.get('.btn-primary').attributes('disabled')).toBeUndefined()
    expect(wrapper.get('.task-resolution-sheet').text()).not.toContain('Complete without note')
    wrapper.unmount()
  })

  it('completes without a resolution when the note is left blank', async () => {
    const wrapper = await mountBoard()
    mockShipDetail()
    apiPost.mockResolvedValue({ workspace: 'personal', task: { ...task({ status: 'done' }), revision: NEXT_REVISION, body: '' } })
    await openResolutionSheet(wrapper)

    await wrapper.get('.task-resolution-sheet form').trigger('submit')
    await flushPromises()
    await nextTick()

    expect(apiPost).toHaveBeenCalledTimes(1)
    expect(apiPost).toHaveBeenCalledWith('/api/tasks/ship/complete', {
      workspace: 'personal',
      expected_revision: NEXT_REVISION,
      resolution: '',
    })
    wrapper.unmount()
  })

  it('completes without a resolution when the note is only whitespace', async () => {
    const wrapper = await mountBoard()
    mockShipDetail()
    apiPost.mockResolvedValue({ workspace: 'personal', task: { ...task({ status: 'done' }), revision: NEXT_REVISION, body: '' } })
    await openResolutionSheet(wrapper)
    await wrapper.get('#task-resolution-text').setValue('   \n\t ')
    await wrapper.get('.task-resolution-sheet form').trigger('submit')
    await flushPromises()
    await nextTick()

    expect(apiPost).toHaveBeenCalledTimes(1)
    expect(apiPost).toHaveBeenCalledWith('/api/tasks/ship/complete', {
      workspace: 'personal',
      expected_revision: NEXT_REVISION,
      resolution: '',
    })
    wrapper.unmount()
  })

  it('preserves a nonblank note exactly as typed', async () => {
    const wrapper = await mountBoard()
    mockShipDetail()
    apiPost.mockResolvedValue({ workspace: 'personal', task: { ...task({ status: 'done' }), revision: NEXT_REVISION, body: '' } })
    await openResolutionSheet(wrapper)
    await wrapper.get('#task-resolution-text').setValue('  Checked the export by hand  \n')
    await wrapper.get('.task-resolution-sheet form').trigger('submit')
    await flushPromises()
    await nextTick()

    expect(apiPost).toHaveBeenCalledTimes(1)
    expect((apiPost.mock.calls[0]![1] as Record<string, unknown>).resolution)
      .toBe('  Checked the export by hand  \n')
    wrapper.unmount()
  })

  it('leaves edit-resolution text untouched, including a whitespace-only draft', async () => {
    const done = task({
      id: 'ship', status: 'done', completed_at: '2026-03-01T12:00:00+00:00', has_resolution: true,
    })
    const wrapper = await mountBoard([done])
    const read = {
      workspace: 'personal',
      task: {
        ...done, revision: NEXT_REVISION, body: '', resolution: 'Checked it',
        completions: [{ id: 'a'.repeat(32), completed_at: '2026-03-01T12:00:00+00:00', resolution: 'Checked it', edited_at: null, attempt_id: '' }],
        delegation_log: '',
      },
    }
    apiGet.mockImplementation((url: string) => Promise.resolve(
      url.startsWith('/api/tasks/ship?') ? read
        : url.includes('/api/tasks?') ? { workspace: 'personal', tasks: [done] } : {},
    ))

    apiPatch.mockResolvedValue({
      workspace: 'personal',
      task: { ...done, revision: THIRD_REVISION, body: '', resolution: '  ' },
    })

    await card(wrapper, 'Ship the board').get('.task-open').trigger('click')
    await flushPromises()
    await nextTick()
    await wrapper.get('.task-resolution').findAll('button').find((b) => b.text() === 'Edit resolution')!.trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.get('.task-resolution-sheet').text()).toContain('Edit resolution')
    expect(wrapper.get('.task-resolution-sheet .form-actions').findAll('button').map((b) => b.text()))
      .toEqual(['Save resolution', 'Cancel'])
    await wrapper.get('#task-resolution-text').setValue('  ')
    await wrapper.get('.task-resolution-sheet form').trigger('submit')
    await flushPromises()
    await nextTick()

    expect(apiPatch).toHaveBeenCalledWith('/api/tasks/ship', {
      workspace: 'personal',
      expected_revision: NEXT_REVISION,
      resolution: '  ',
    })
    expect(apiPost).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('keeps the resolution draft when the complete request conflicts, and Refresh task keeps it too', async () => {
    const wrapper = await mountBoard()
    mockShipDetail()
    apiPost.mockRejectedValue(Object.assign(new Error('HTTP 409'), {
      payload: { error: { code: 'task_revision_conflict', message: 'that task changed on disk', retryable: true } },
    }))
    await openResolutionSheet(wrapper)
    await wrapper.get('#task-resolution-text').setValue('Draft note')
    await wrapper.get('.task-resolution-sheet form').trigger('submit')
    await flushPromises()
    await nextTick()

    expect(apiPost).toHaveBeenCalledTimes(1)
    expect(wrapper.get('.task-resolution-sheet .task-action-error').text()).toContain('that task changed on disk')
    expect(wrapper.find('#task-detail-name').exists()).toBe(true)
    expect((wrapper.get('#task-resolution-text').element as HTMLTextAreaElement).value).toBe('Draft note')

    // Refresh task re-reads the board and the task; the draft is still there and
    // no second write went out on its own.
    await wrapper.get('.task-resolution-sheet').findAll('button').find((b) => b.text() === 'Refresh task')!.trigger('click')
    await flushPromises()
    await nextTick()
    expect((wrapper.get('#task-resolution-text').element as HTMLTextAreaElement).value).toBe('Draft note')
    expect(wrapper.find('.task-resolution-sheet .task-action-error').exists()).toBe(false)
    expect(apiPost).toHaveBeenCalledTimes(1)

    // The retained draft is retryable: the next submit sends it unchanged.
    apiPost.mockResolvedValue({
      workspace: 'personal',
      task: { ...task({ status: 'done' }), revision: THIRD_REVISION, body: '' },
    })
    await wrapper.get('.task-resolution-sheet form').trigger('submit')
    await flushPromises()
    await nextTick()
    expect(apiPost).toHaveBeenCalledTimes(2)
    expect(apiPost.mock.calls[1]![1]).toMatchObject({ expected_revision: NEXT_REVISION, resolution: 'Draft note' })
    expect(wrapper.find('.task-resolution-sheet').exists()).toBe(false)
    wrapper.unmount()
  })

  it('offers a Reload beside a refused write, and recovers from the 409', async () => {
    const wrapper = await mountBoard()
    apiPatch.mockRejectedValue(Object.assign(new Error('HTTP 409'), {
      payload: { error: { code: 'task_revision_conflict', message: 'that task changed on disk', retryable: true } },
    }))
    await card(wrapper, 'Ship the board').get('.task-open').trigger('keydown', { key: 'ArrowRight', shiftKey: true })
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
    expect(card(wrapper, 'First task').text()).toContain(shortDate(2099, 4, 1))
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
    askConfirmReason.mockResolvedValue({ confirmed: false, reason: '' })
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

    askConfirmReason.mockResolvedValue({ confirmed: true, reason: '' })
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
  // A wide window can still have a narrow pane with the sidebar open.
  it('keeps the four lanes side by side, with column gestures on, when the pane narrows', async () => {
    const original = window.innerWidth
    Object.defineProperty(window, 'innerWidth', { value: 1600, configurable: true })
    try {
      const wrapper = await mountBoard()
      reportPaneWidth(1200)
      await nextTick()
      expect(lanes(wrapper)).toHaveLength(4)

      // Narrow pane, wide window: the same four groups, still one row. The board
      // scrolls them sideways; nothing is stacked and no gesture is switched off.
      reportPaneWidth(700)
      await nextTick()
      expect(lanes(wrapper).map(lane => lane.get('.task-lane-label').text()))
        .toEqual(['To do', 'In progress', 'In review', 'Done'])
      expect(wrapper.findAll('.task-lanes')).toHaveLength(1)
      expect(wrapper.findAll('.task-card')).toHaveLength(4)
      expect(wrapper.findAll('.task-card').every(card => card.find('.drag-grip').exists())).toBe(true)
      expect(wrapper.get('.task-open').attributes('aria-keyshortcuts')).toBe('Shift+ArrowLeft Shift+ArrowRight')
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

  /** The card's one band action. */
  function bandAction(wrapper: ReturnType<typeof mount>, title: string) {
    return card(wrapper, title).find('.task-agent-action')
  }

  /** Open the card's menu and return its items. */
  async function menuItems(wrapper: ReturnType<typeof mount>, title: string) {
    await card(wrapper, title).get('.task-more').trigger('click')
    await flushPromises()
    // The attempt gestures; Move up / Move down are on every card and have their own tests.
    return [...document.body.querySelectorAll<HTMLElement>('[data-reka-menu-content] [role="menuitem"]')]
      .filter((el) => el.textContent !== 'Move up' && el.textContent !== 'Move down')
  }

  /** Close an open card menu the way a click outside does. */
  async function closeMenu() {
    document.body.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true, button: 0 }))
    await flushPromises()
  }

  /** The card's control with this label: its band action (arrow left off) or a menu item. */
  async function chip(wrapper: ReturnType<typeof mount>, title: string, label: string) {
    const action = bandAction(wrapper, title)
    if (action.exists() && action.text().replace(/\s*→$/, '') === label) return action
    const item = (await menuItems(wrapper, title)).find((el) => el.textContent === label)!
    return new DOMWrapper(item)
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

  it('draws the agent band for every state, toned by what it means',
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

      const band = (title: string) => card(wrapper, title).get('.task-agent-band')
      // In flight, waiting on the user, ready to review, and over: four tones,
      // because a paused turn and a failed one are different facts.
      expect(band('Wire the store').text()).toContain('Agent working')
      expect(band('Wire the store').classes()).toContain('task-agent-band--running')
      expect(band('Waiting on you').text()).toContain('Needs you')
      expect(band('Waiting on you').classes()).toContain('task-agent-band--needs')
      expect(band('Dead turn').text()).toContain('Agent failed')
      expect(band('Dead turn').classes()).toContain('task-agent-band--settled')
      // A done card has no band, even while its attempt still holds it.
      expect(card(wrapper, 'Finished').find('.task-agent-band').exists()).toBe(false)
      // A task nobody delegated has no band and no menu.
      expect(card(wrapper, 'Never delegated').find('.task-agent-band').exists()).toBe(false)
      expect(await menuItems(wrapper, 'Never delegated')).toEqual([])
      await closeMenu()
      wrapper.unmount()
    })

  describe('decision reasons (#1730)', () => {
    const reviewed = () => liveTask({ attempt_state: 'ready_for_review', revision: THIRD_REVISION })

    async function openDetail(wrapper: ReturnType<typeof mount>, title: string) {
      await card(wrapper, title).get('.task-open').trigger('click')
      await flushPromises()
      await nextTick()
    }

    function detailButton(wrapper: ReturnType<typeof mount>, label: string) {
      return wrapper.get('.task-sheet').findAll('button').find((b) => b.text() === label)
    }

    it('delete sends the reason the confirm collected, and none when it is skipped', async () => {
      apiDel.mockResolvedValue({ workspace: 'personal', deleted: 'ship' })
      const wrapper = await mountBoard()
      askConfirmReason.mockResolvedValue({ confirmed: true, reason: 'not mine' })
      await openDetail(wrapper, 'Ship the board')
      await wrapper.get('.task-delete').trigger('click')
      await flushPromises()
      expect(askConfirmReason).toHaveBeenCalledTimes(1)
      expect(apiDel).toHaveBeenCalledWith('/api/tasks/ship', {
        workspace: 'personal', expected_revision: REVISION, reason: 'not mine',
      })
      wrapper.unmount()

      apiDel.mockClear()
      askConfirmReason.mockResolvedValue({ confirmed: true, reason: '' })
      const again = await mountBoard()
      await openDetail(again, 'Ship the board')
      await again.get('.task-delete').trigger('click')
      await flushPromises()
      // One click, as before: no reason key at all.
      expect(apiDel).toHaveBeenCalledWith('/api/tasks/ship', {
        workspace: 'personal', expected_revision: REVISION,
      })
      again.unmount()
    })

    it('shows the reason block only on the sheet of a review-ready task', async () => {
      const wrapper = await mountWithBody([reviewed()])
      await card(wrapper, 'Wire the store').get('.task-check').trigger('click')
      await flushPromises()
      expect(wrapper.get('.task-resolution-sheet').find('.decision-reason').exists()).toBe(true)
      wrapper.unmount()

      const plain = await mountWithBody([liveTask({ attempt_state: 'running' }), plainTask()])
      await card(plain, 'Draft the runbook').get('.task-check').trigger('click')
      await flushPromises()
      expect(plain.get('.task-resolution-sheet').find('.decision-reason').exists()).toBe(false)
      plain.unmount()
    })

    it('approving sends the typed reason with the completion', async () => {
      apiPost.mockResolvedValue({
        workspace: 'personal',
        task: { ...reviewed(), status: 'done', attempt_id: '', chat_id: '', live_attempt_id: '', revision: NEXT_REVISION },
      })
      const wrapper = await mountWithBody([reviewed()])
      await card(wrapper, 'Wire the store').get('.task-check').trigger('click')
      await flushPromises()
      const sheet = wrapper.get('.task-resolution-sheet')
      await sheet.findAll('.decision-reason .task-chip').find((b) => b.text() === 'good as is')!.trigger('click')
      await sheet.get('form').trigger('submit')
      await flushPromises()
      expect(apiPost).toHaveBeenCalledWith('/api/tasks/doing/complete', {
        workspace: 'personal',
        expected_revision: THIRD_REVISION,
        resolution: '',
        reason: 'good as is',
      })
      wrapper.unmount()
    })

    it('a failed approval keeps the typed reason in the sheet', async () => {
      apiPost.mockRejectedValue(new Error('conflict'))
      const wrapper = await mountWithBody([reviewed()])
      await card(wrapper, 'Wire the store').get('.task-check').trigger('click')
      await flushPromises()
      const field = () => wrapper.get<HTMLTextAreaElement>('#task-approve-reason')
      await field().setValue('looked right')
      await wrapper.get('.task-resolution-sheet form').trigger('submit')
      await flushPromises()
      await nextTick()
      expect(wrapper.find('.task-resolution-sheet').exists()).toBe(true)
      expect(field().element.value).toBe('looked right')
      wrapper.unmount()
    })

    it('offers Reject only for a review-ready task, and detaches with the reason', async () => {
      apiPost.mockResolvedValue({
        workspace: 'personal',
        attempt: { ...delegateAnswer().attempt, attempt_id: LIVE_ID, state: 'stopped', live: false },
      })
      const running = await mountWithBody([liveTask()])
      await openDetail(running, 'Wire the store')
      expect(detailButton(running, 'Reject…')).toBeUndefined()
      running.unmount()

      const wrapper = await mountWithBody([reviewed()])
      await openDetail(wrapper, 'Wire the store')
      askConfirmReason.mockResolvedValue({ confirmed: true, reason: 'not a task' })
      await detailButton(wrapper, 'Reject…')!.trigger('click')
      await flushPromises()
      expect(askConfirmReason.mock.calls[0]![1]).toMatchObject({ title: 'Reject result', confirmLabel: 'Reject' })
      expect(apiPost).toHaveBeenCalledWith(
        `/api/tasks/doing/attempt/${LIVE_ID}/detach`,
        { workspace: 'personal', reason: 'not a task' },
      )
      wrapper.unmount()
    })

    it('cancelling the Reject confirm sends nothing, and plain Detach stays one click with no reason', async () => {
      const wrapper = await mountWithBody([reviewed()])
      await openDetail(wrapper, 'Wire the store')
      askConfirmReason.mockResolvedValue({ confirmed: false, reason: '' })
      await detailButton(wrapper, 'Reject…')!.trigger('click')
      await flushPromises()
      expect(apiPost).not.toHaveBeenCalled()
      wrapper.unmount()

      apiPost.mockResolvedValue({
        workspace: 'personal',
        attempt: { ...delegateAnswer().attempt, attempt_id: LIVE_ID, state: 'stopped', live: false },
      })
      const running = await mountWithBody([liveTask()])
      await openDetail(running, 'Wire the store')
      await detailButton(running, 'Detach')!.trigger('click')
      await flushPromises()
      expect(askConfirmReason).toHaveBeenCalledTimes(1)
      expect(apiPost).toHaveBeenCalledWith(
        `/api/tasks/doing/attempt/${LIVE_ID}/detach`,
        { workspace: 'personal' },
      )
      running.unmount()
    })
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
    expect(delegated.get('.task-agent-band').text()).toContain('Agent working')
    // And the gesture set replaced the single Delegate control.
    const labels = (await menuItems(wrapper, 'Draft the runbook')).map((el) => el.textContent)
    expect(labels).toContain('Stop')
    expect(labels).toContain('Detach')
    expect(labels).not.toContain('Delegate')
    wrapper.unmount()
  })

  it('hands over the instructions typed into the sheet with the delegation', async () => {
    apiPost.mockResolvedValue(delegateAnswer())
    const wrapper = await mountWithBody([plainTask({ project_id: 'p1' })])

    await openPreview(wrapper, 'Draft the runbook')
    await wrapper.get('#task-delegate-instructions').setValue('Ask before deleting. Done = a link.')
    await wrapper.get('.task-sheet .btn-primary').trigger('click')
    await flushPromises()

    expect(apiPost).toHaveBeenCalledWith('/api/tasks/fresh/delegate', {
      workspace: 'personal',
      expected_revision: REVISION,
      instructions: 'Ask before deleting. Done = a link.',
    })
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

      expect((await menuItems(wrapper, 'Wire the store')).map((el) => el.textContent))
        .toEqual(['Open chat', 'Stop', 'Detach'])
      expect(bandAction(wrapper, 'Wire the store').text()).toBe('Open chat →')
      await closeMenu()
      // A live attempt holds the task, so there is no Done to tick.
      expect(card(wrapper, 'Wire the store').find('.task-check').exists()).toBe(true)
      // A task with nothing delegated has no attempt gestures and no Chat: the
      // board only links a chat it was told about, and handing over starts in
      // the editor. Its one gesture is the Done checkbox.
      expect(await menuItems(wrapper, 'Never delegated')).toEqual([])
      await closeMenu()
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

    // No attempt gestures, and a plain Done checkbox because there is nothing
    // left to approve.
    expect(await menuItems(wrapper, 'Reviewed, then released')).toEqual([])
    await closeMenu()
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
      expect((await menuItems(wrapper, 'Broke halfway')).map((el) => el.textContent))
        .toEqual(['Open chat', 'Resume', 'Retry'])
      expect(bandAction(wrapper, 'Broke halfway').text()).toBe('Resume →')
      await closeMenu()

      await (await chip(wrapper, 'Broke halfway', 'Resume')).trigger('click')
      await flushPromises()
      expect(apiPost).toHaveBeenCalledWith(
        `/api/tasks/broke/attempt/${SETTLED_ID}/resume`,
        { workspace: 'personal' },
      )

      await (await chip(wrapper, 'Broke halfway', 'Retry')).trigger('click')
      await flushPromises()
      expect(apiPost).toHaveBeenCalledWith(
        `/api/tasks/broke/attempt/${SETTLED_ID}/retry`,
        { workspace: 'personal' },
      )
      wrapper.unmount()
    })

  it('shows Done by agent on a done card the agent finished', async () => {
    const wrapper = await mountWithBody([task({
      id: 'fin', title: 'Shipped it', status: 'done', assignee: 'agent',
      attempt_outcome: 'done', attempt_summary: 'All wired.', due: '2000-01-01',
      completed_at: '2026-03-01T12:00:00+00:00',
    })])
    const shown = card(wrapper, 'Shipped it')
    expect(shown.get('.task-meta').text()).toContain('Done by agent')
    expect(shown.get('.task-meta').text()).toContain(shortDate(2026, 3, 1))
    expect(shown.find('.task-due--over').exists()).toBe(false)
    expect(shown.find('.task-agent-note').exists()).toBe(false)
    expect(shown.find('.task-agent-band').exists()).toBe(false)
    wrapper.unmount()
  })

  it('pins one action to the agent band per state', async () => {
    for (const state of ['running', 'needs_you', 'ready_for_review'] as const) {
      const wrapper = await mountWithBody([liveTask({ attempt_state: state })])
      expect(bandAction(wrapper, 'Wire the store').text(), state).toBe('Open chat →')
      wrapper.unmount()
    }
    const stopped = await mountWithBody([settledTask({ attempt_state: 'stopped' })])
    expect(bandAction(stopped, 'Broke halfway').text()).toBe('Resume →')
    stopped.unmount()
    const archived = await mountWithBody([settledTask({ chat_archived: true })])
    expect(bandAction(archived, 'Broke halfway').text()).toBe('Continue in new chat →')
    archived.unmount()
  })

  it('acts from the card menu without opening the editor', async () => {
    apiPost.mockResolvedValue({
      workspace: 'personal',
      attempt: { ...delegateAnswer().attempt, attempt_id: LIVE_ID, chat_id: 'chat-7', state: 'stopped', live: false },
      chat_id: 'chat-7',
    })
    const wrapper = await mountWithBody([liveTask()])
    await (await chip(wrapper, 'Wire the store', 'Stop')).trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith(
      `/api/tasks/doing/attempt/${LIVE_ID}/stop`,
      { workspace: 'personal' },
    )
    expect(wrapper.find('.task-sheet').exists()).toBe(false)
    wrapper.unmount()
  })

  it('opens the card menu without opening the editor', async () => {
    const wrapper = await mountWithBody([liveTask()])
    expect((await menuItems(wrapper, 'Wire the store')).length).toBeGreaterThan(0)
    expect(wrapper.find('.task-sheet').exists()).toBe(false)
    expect(card(wrapper, 'Wire the store').get('.task-more').attributes('aria-label'))
      .toBe('Actions for Wire the store')
    await closeMenu()
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

    await (await chip(wrapper, 'Wire the store', 'Stop')).trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith(
      `/api/tasks/doing/attempt/${LIVE_ID}/stop`,
      { workspace: 'personal' },
    )
    wrapper.unmount()

    // Detach is a gesture on a *live* attempt, so it is read on a card that still
    // has one — a stopped attempt has nothing running to stop before releasing.
    const releasing = await mountWithBody([liveTask()])
    await (await chip(releasing, 'Wire the store', 'Detach')).trigger('click')
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

      await (await chip(wrapper, 'Wire the store', 'Stop')).trigger('click')
      await flushPromises()

      expect(card(wrapper, 'Wire the store').get('.task-agent-band').text()).toContain('Agent stopped')
      expect((await menuItems(wrapper, 'Wire the store')).map((el) => el.textContent))
        .toEqual(['Open chat', 'Resume', 'Retry'])
      await closeMenu()
      wrapper.unmount()
    })

  it('approves a reviewed result into Done in one gesture, and keeps the attempt',
    async () => {
      // The review is the case the issue describes, and the approve checkbox is the
      // control for it: one request, no Detach first, and the service releases the linkage
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
          // The completion just recorded its stamp, as the server does.
          completed_at: '2026-03-01T12:00:00+00:00',
          chat_id: '',
          attempt_id: '',
          attempt_state: 'ready_for_review',
          live_attempt_id: '',
          revision: NEXT_REVISION,
        },
      })
      const wrapper = await mountWithBody([reviewed])

      // No Stop and no Detach on a card whose turn has ended: there is nothing
      // running to stop. The menu only opens the chat; approving is the card's
      // dashed checkbox, which opens the completion sheet.
      const shown = card(wrapper, 'Wire the store')
      expect((await menuItems(wrapper, 'Wire the store')).map((el) => el.textContent)).toEqual(['Open chat'])
      await closeMenu()
      expect(shown.get('.task-agent-band').classes()).toContain('task-agent-band--review')
      const approve = shown.get('.task-check')
      expect(approve.classes()).toContain('task-check--approve')
      expect(approve.attributes('aria-label')).toBe('Approve and mark done')
      await approve.trigger('click')
      await flushPromises()
      // The review-ready sheet keeps its Approve done label, and the note is
      // optional there too: one primary action plus Cancel.
      const sheet = wrapper.get('.task-resolution-sheet')
      expect(sheet.findAll('.form-actions button').map((b) => b.text()))
        .toEqual(['Approve done', 'Cancel'])
      expect(sheet.get('.form-actions .btn-primary').attributes('disabled')).toBeUndefined()
      await sheet.get('form').trigger('submit')
      await flushPromises()

      // One request, the completion route — no attempt gesture in between.
      expect(apiPost).toHaveBeenCalledTimes(1)
      expect(apiPost).toHaveBeenCalledWith('/api/tasks/doing/complete', {
        workspace: 'personal',
        expected_revision: THIRD_REVISION,
        resolution: '',
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

      // The agent's own summary of what it did; the band says it is ready to review.
      const shown = card(wrapper, 'Wire the store')
      expect(shown.get('.task-agent-band').text()).toContain('Ready to review')
      expect(bandAction(wrapper, 'Wire the store').text()).toBe('Open chat →')
      expect(shown.get('.task-agent-note').text()).toBe('Wired the store; tests in store.test.ts.')

      await shown.get('.task-open').trigger('click')
      await flushPromises()
      await nextTick()
      const control = wrapper.findAll('.task-delegate-actions .btn-chip')
        .find((c) => c.text() === 'Approve Done')
      expect(control, 'Approve Done in the editor').toBeTruthy()
      await control!.trigger('click')
      await flushPromises()
      await wrapper.get('.task-resolution-sheet form').trigger('submit')
      await flushPromises()

      expect(apiPost).toHaveBeenCalledWith('/api/tasks/doing/complete', {
        workspace: 'personal',
        expected_revision: THIRD_REVISION,
        resolution: '',
      })
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
    expect(shown.get('.task-agent-band').text()).toContain('Needs you')
    expect(shown.get('.task-agent-note').text()).toContain('without a report')
    wrapper.unmount()
  })

  it('offers to continue an archived chat\'s attempt in a new chat, never to resume it', async () => {
    // After a reload the browser's chat list holds no archived chat, so the answer
    // has to come from the server's `chat_archived` on the row.
    expect(useProjectStore().chats).toEqual([])
    const wrapper = await mountWithBody([settledTask({
      attempt_outcome: 'blocked', attempt_summary: 'Schema done; need creds.',
      attempt_detail: 'the chat was archived', chat_archived: true,
    })])
    const shown = card(wrapper, 'Broke halfway')
    const labels = (await menuItems(wrapper, 'Broke halfway')).map((el) => el.textContent)
    expect(labels).not.toContain('Resume')
    expect(labels).toContain('Continue in new chat')
    expect(bandAction(wrapper, 'Broke halfway').text()).toBe('Continue in new chat →')
    await closeMenu()
    expect(shown.text()).toContain('The chat was archived')
    expect(shown.get('.task-agent-note').text()).toBe('Schema done; need creds.')

    await shown.get('.task-open').trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.get('.task-delegate-actions .task-chip').text()).toBe('Continue in a new chat')
    wrapper.unmount()
  })

  it('offers a new chat when the chat was archived after the board loaded', async () => {
    // Archiving does not re-publish the board's rows, so the server flag still
    // says false here. The live chat list is what knows the archive happened.
    useProjectStore().chats = [{ chat_id: 'chat-7', project_id: 'p1', archived: true } as never]
    const wrapper = await mountWithBody([settledTask({ chat_archived: false })])
    const labels = (await menuItems(wrapper, 'Broke halfway')).map((el) => el.textContent)
    expect(labels).not.toContain('Resume')
    expect(labels).toContain('Continue in new chat')
    wrapper.unmount()
  })

  it('keeps Resume while the live chat is active and the server flag is false', async () => {
    useProjectStore().chats = [{ chat_id: 'chat-7', project_id: 'p1', archived: false } as never]
    const wrapper = await mountWithBody([settledTask({ chat_archived: false })])
    const labels = (await menuItems(wrapper, 'Broke halfway')).map((el) => el.textContent)
    expect(labels).toEqual(['Open chat', 'Resume', 'Retry'])
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


  it('the card checkbox unlinks a settled attempt, after asking, then completes', async () => {
    const wrapper = await mountWithBody([settledTask()])
    apiPost.mockImplementation((url: string) => Promise.resolve(
      url.endsWith('/detach')
        ? { workspace: 'personal', task: { ...settledTask({ attempt_id: '', chat_id: '' }), revision: REVISION }, attempt: { attempt_id: SETTLED_ID, task_id: 'broke', state: 'interrupted' } }
        : { workspace: 'personal', task: { ...settledTask({ status: 'done', attempt_id: '' }), revision: REVISION, body: '' } },
    ))

    await card(wrapper, 'Broke halfway').get('.task-check').trigger('click')
    await flushPromises()
    await wrapper.get('.task-resolution-sheet form').trigger('submit')
    await flushPromises()

    expect(askConfirm).toHaveBeenCalledTimes(1)
    expect(String(askConfirm.mock.calls[0]![0])).toContain('still linked to its last attempt (Interrupted)')
    const posts = apiPost.mock.calls.map((c) => String(c[0]))
    expect(posts).toEqual([`/api/tasks/broke/attempt/${SETTLED_ID}/detach`, '/api/tasks/broke/complete'])
    wrapper.unmount()
  })

  it('the card checkbox says a refusal in a toast, not below the columns', async () => {
    const wrapper = await mountWithBody([task({ id: 'plain', title: 'Never delegated' })])
    const toast = vi.fn()
    useProjectStore().pushErrorToast = toast
    apiPost.mockRejectedValue(new Error('the task store is busy'))

    await card(wrapper, 'Never delegated').get('.task-check').trigger('click')
    await flushPromises()
    await wrapper.get('.task-resolution-sheet form').trigger('submit')
    await flushPromises()

    expect(askConfirm).not.toHaveBeenCalled()
    expect(wrapper.get('.task-resolution-sheet .task-action-error').text()).toContain('the task store is busy')
    expect(toast).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('the editor\'s Open chat goes to the chat, not to a preview under the editor', async () => {
    const wrapper = await mountWithBody([liveTask({ attempt_state: 'needs_you' })])
    const switchChat = vi.fn()
    useProjectStore().switchChat = switchChat
    await card(wrapper, 'Wire the store').get('.task-open').trigger('click')
    await flushPromises()
    await nextTick()
    const control = wrapper.get('.task-delegate-actions .task-chip')
    expect(control.text()).toBe('Open chat')

    await control.trigger('click')
    await flushPromises()
    await nextTick()

    expect(switchChat).toHaveBeenCalledWith('chat-7')
    expect(wrapper.find('#task-detail-title').exists()).toBe(false)
    expect(wrapper.find('#task-delegate-title').exists()).toBe(false)
    wrapper.unmount()
  })

  it('Done on a task an open attempt holds detaches it first, after asking', async () => {
    const wrapper = await mountWithBody([liveTask({ attempt_state: 'needs_you' })])
    await card(wrapper, 'Wire the store').get('.task-open').trigger('click')
    await flushPromises()
    await nextTick()
    apiPost.mockImplementation((url: string) => Promise.resolve(
      url.endsWith('/detach')
        ? { workspace: 'personal', task: { ...liveTask({ attempt_state: 'needs_you', live_attempt_id: '', chat_id: '' }), revision: REVISION }, attempt: { attempt_id: LIVE_ID, task_id: 'doing', state: 'needs_you' } }
        : { workspace: 'personal', task: { ...liveTask({ status: 'done', live_attempt_id: '' }), revision: REVISION, body: '' } },
    ))

    await wrapper.get('.task-status-opt[data-status="done"]').trigger('click')
    await flushPromises()
    await wrapper.get('.task-resolution-sheet form').trigger('submit')
    await flushPromises()

    expect(askConfirm).toHaveBeenCalledTimes(1)
    const posts = apiPost.mock.calls.map((c) => String(c[0]))
    expect(posts[0]).toBe(`/api/tasks/doing/attempt/${LIVE_ID}/detach`)
    expect(posts[1]).toBe('/api/tasks/doing/complete')
    wrapper.unmount()
  })

  it('Done says a refusal beside the status control', async () => {
    const wrapper = await mountWithBody([liveTask({ attempt_state: 'needs_you' })])
    await card(wrapper, 'Wire the store').get('.task-open').trigger('click')
    await flushPromises()
    await nextTick()
    askConfirm.mockResolvedValue(true)
    apiPost.mockRejectedValue(new Error('the attempt could not be detached'))

    await wrapper.get('.task-status-opt[data-status="done"]').trigger('click')
    await flushPromises()
    await wrapper.get('.task-resolution-sheet form').trigger('submit')
    await flushPromises()

    expect(wrapper.get('.task-resolution-sheet .task-action-error').text()).toContain('the attempt could not be detached')
    wrapper.unmount()
  })

  it('opens the attempt chat through the project store, not a route', async () => {
    const wrapper = await mountWithBody([liveTask()])
    const switchChat = vi.fn()
    // The sidebar row and the in-app toast both go through this; a delegation
    // specific route would be a second path to a chat and would drift.
    useProjectStore().switchChat = switchChat

    await (await chip(wrapper, 'Wire the store', 'Open chat')).trigger('click')

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

  describe('the /tasks/<id> address', () => {
    async function mountAt(path: string) {
      const { createMemoryHistory, createRouter } = await import('vue-router')
      const { defineComponent } = await import('vue')
      const Stub = defineComponent({ render: () => null })
      const router = createRouter({
        history: createMemoryHistory(),
        routes: [
          { path: '/tasks', name: 'tasks', component: Stub },
          { path: '/tasks/:taskId', name: 'task-detail', component: Stub },
        ],
      })
      await router.push(path)
      apiGet.mockImplementation((url: string) => Promise.resolve(
        url.includes('/api/tasks?')
          ? { workspace: 'personal', tasks: BOARD }
          : detailAnswer(task(), 'Linked from a chat.'),
      ))
      const wrapper = mount(TaskBoardView, { attachTo: document.body, global: { plugins: [router] } })
      await flushPromises()
      await nextTick()
      return { wrapper, router }
    }

    it('opens the linked task once the board has loaded', async () => {
      const { wrapper, router } = await mountAt('/tasks/ship')
      const sheet = wrapper.get('.task-sheet')
      expect(sheet.get<HTMLInputElement>('#task-detail-name').element.value).toBe('Ship the board')
      expect(router.currentRoute.value.path).toBe('/tasks/ship')

      window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
      await flushPromises()
      await nextTick()
      expect(wrapper.find('.task-sheet').exists()).toBe(false)
      expect(router.currentRoute.value.path).toBe('/tasks')
      wrapper.unmount()
    })

    it('keeps the address of the task opened from the board', async () => {
      const { wrapper, router } = await mountAt('/tasks')
      await card(wrapper, 'Wait on a key').get('.task-open').trigger('click')
      await flushPromises()
      expect(router.currentRoute.value.path).toBe('/tasks/held')
      wrapper.unmount()
    })

    it('says so when the linked task is not on this board, and drops the address', async () => {
      const { wrapper, router } = await mountAt('/tasks/gone')
      expect(wrapper.find('.task-sheet').exists()).toBe(false)
      expect(wrapper.text()).toContain('The linked task is not on the personal board.')
      expect(router.currentRoute.value.path).toBe('/tasks')
      wrapper.unmount()
    })

    it('opens a linked task that only the refresh returns, not the stale rows', async () => {
      const { createMemoryHistory, createRouter } = await import('vue-router')
      const { defineComponent } = await import('vue')
      // The board loaded before the task existed; the store keeps those rows.
      apiGet.mockImplementation((url: string) =>
        Promise.resolve(url.includes('/api/tasks?') ? { workspace: 'personal', tasks: BOARD } : {}),
      )
      await useTaskBoardStore().reload('personal')

      // The remount's refresh answers only once the test lets it, so the
      // assertions below run while it is in flight.
      const fresh = task({ id: 'fresh', title: 'Made since the board loaded' })
      let answerRefresh!: (value: unknown) => void
      const refresh = new Promise((resolve) => { answerRefresh = resolve })
      apiGet.mockImplementation((url: string) => (
        url.includes('/api/tasks?')
          ? refresh
          : Promise.resolve(detailAnswer(fresh, 'Created by an agent.'))
      ))

      const Stub = defineComponent({ render: () => null })
      const router = createRouter({
        history: createMemoryHistory(),
        routes: [
          { path: '/tasks', name: 'tasks', component: Stub },
          { path: '/tasks/:taskId', name: 'task-detail', component: Stub },
        ],
      })
      await router.push('/tasks/fresh')
      const wrapper = mount(TaskBoardView, { attachTo: document.body, global: { plugins: [router] } })
      await flushPromises()
      await nextTick()

      expect(wrapper.text()).not.toContain('The linked task is not on the personal board.')
      expect(router.currentRoute.value.path).toBe('/tasks/fresh')

      answerRefresh({ workspace: 'personal', tasks: [...BOARD, fresh] })
      await flushPromises()
      await nextTick()

      const sheet = wrapper.get('.task-sheet')
      expect(sheet.get<HTMLInputElement>('#task-detail-name').element.value).toBe('Made since the board loaded')
      expect(router.currentRoute.value.path).toBe('/tasks/fresh')
      wrapper.unmount()
    })
  })

  describe('placing cards (#2092)', () => {
    const PLACED: TaskRow[] = [
      task({ id: 'a', title: 'Alpha', status: 'backlog' }),
      task({ id: 'b', title: 'Bravo', status: 'backlog' }),
      task({ id: 'c', title: 'Charlie', status: 'backlog' }),
      task({ id: 'd', title: 'Delta', status: 'in_progress' }),
      task({ id: 'e', title: 'Echo', status: 'in_progress' }),
      task({ id: 'g', title: 'Golf', status: 'done' }),
      task({ id: 'h', title: 'Hotel', status: 'done' }),
    ]
    const SEQUENCES = {
      backlog: ['a', 'b', 'c'], in_progress: ['d', 'e'], in_review: [] as string[], done: ['g', 'h'],
    }
    const orderPayload = (version = 7, sequences: Record<string, string[]> = SEQUENCES) => ({ version, sequences })

    async function mountPlaced(extra: Record<string, unknown> = {}, rows: TaskRow[] = PLACED) {
      apiGet.mockImplementation((url: string) =>
        Promise.resolve(
          url.includes('/api/tasks?')
            ? { workspace: 'personal', tasks: rows, order: orderPayload(), ...extra }
            : {},
        ),
      )
      const wrapper = mount(TaskBoardView, { attachTo: document.body })
      await flushPromises()
      await nextTick()
      layOutBoard()
      return wrapper
    }

    /** What the server answers a placement write with: the card, and the new order. */
    function placed(id: string, sequences: Record<string, string[]>, status = 'backlog', version = 8) {
      const row = PLACED.find((t) => t.id === id) as Task
      return {
        workspace: 'personal',
        task: {
          ...row,
          status: status as Task['status'],
          completed_at: status === 'done' ? '2026-03-01T08:00:00Z' : null,
          revision: NEXT_REVISION,
          body: '',
        },
        order: orderPayload(version, sequences),
      }
    }

    const conflict = (code: string) => Object.assign(new Error('HTTP 409'), {
      payload: { error: { code, message: 'stale', retryable: true } },
    })
    const boardReads = () => apiGet.mock.calls.filter((c) => String(c[0]).startsWith('/api/tasks?')).length
    const grip = (wrapper: ReturnType<typeof mount>, title: string) => card(wrapper, title).get('.drag-grip')
    const key = (k: string, init: KeyboardEventInit = {}) => ({ key: k, ...init })

    /** Press the card's own text (not its grip) and move far enough to pick it up. */
    async function pickUpSurface(wrapper: ReturnType<typeof mount>, title: string) {
      layOutBoard()
      const at = card(wrapper, title).element.getBoundingClientRect()
      const text = card(wrapper, title).get('.task-title').element
      text.dispatchEvent(pointer('pointerdown', at.left + 20, at.top + 10))
      boardRoot().dispatchEvent(pointer('pointermove', at.left + 20, at.top + 24))
      await nextTick()
    }

    async function openMenu(wrapper: ReturnType<typeof mount>, title: string) {
      await card(wrapper, title).get('.task-more').trigger('click')
      await flushPromises()
      return [...document.body.querySelectorAll<HTMLElement>('[data-reka-menu-content] [role="menuitem"]')]
    }
    const byLabel = (items: HTMLElement[], label: string) => items.find((el) => el.textContent === label)!
    async function closeAnyMenu() {
      document.body.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true, button: 0 }))
      await flushPromises()
    }

    beforeEach(() => layOutBoard())

    it('reorders within a lane by dragging the grip and persists once via the order route', async () => {
      const wrapper = await mountPlaced()
      expect(cardTitles(wrapper, 0)).toEqual(['Alpha', 'Bravo', 'Charlie'])
      apiPost.mockResolvedValueOnce(placed('c', { ...SEQUENCES, backlog: ['c', 'a', 'b'] }))

      await dragCardTo(wrapper, 'Charlie', 0, 0)

      expect(apiPost).toHaveBeenCalledTimes(1)
      expect(apiPost).toHaveBeenCalledWith('/api/tasks/c/order', {
        workspace: 'personal', before_id: 'a', after_id: null, expected_order_version: 7,
      })
      expect(apiPatch).not.toHaveBeenCalled()
      expect(cardTitles(wrapper, 0)).toEqual(['Charlie', 'Alpha', 'Bravo'])
      expect(document.querySelector('.drag-overlay')).toBeNull()

      // Middle to last: the anchor is the card it lands after.
      apiPost.mockResolvedValueOnce(placed('a', { ...SEQUENCES, backlog: ['c', 'b', 'a'] }, 'backlog', 9))
      await dragCardTo(wrapper, 'Alpha', 0, 99)
      expect(apiPost).toHaveBeenCalledTimes(2)
      expect(apiPost).toHaveBeenLastCalledWith('/api/tasks/a/order', {
        workspace: 'personal', before_id: null, after_id: 'b', expected_order_version: 8,
      })
      expect(cardTitles(wrapper, 0)).toEqual(['Charlie', 'Bravo', 'Alpha'])
      wrapper.unmount()
    })

    it('shows the live displacement while dragging: a placeholder opens where the card would land', async () => {
      const wrapper = await mountPlaced()
      await pickUp(wrapper, 'Alpha')
      expect(document.querySelector('.drag-overlay')).not.toBeNull()
      expect(card(wrapper, 'Alpha').classes()).toContain('drag-source')
      const at = dropPoint(1, 1)
      await dragTo(at.x, at.y)
      expect(lanes(wrapper)[1]!.findAll('.task-cards > li').map((li) => li.classes().includes('drag-placeholder')))
        .toEqual([false, true, false])
      expect(lanes(wrapper)[1]!.classes()).toContain('task-lane--drop')
      expect(boardRoot().style.getPropertyValue('--drag-item-height')).toBe('40px')
      await release(-500, -500)
      expect(apiPatch).not.toHaveBeenCalled()
      wrapper.unmount()
    })

    it('drops into an empty lane and into an off-screen destination lane', async () => {
      const wrapper = await mountPlaced()
      reportPaneWidth(390)
      await nextTick()
      apiPatch.mockResolvedValueOnce(placed('a', { ...SEQUENCES, backlog: ['b', 'c'], in_review: ['a'] }, 'in_review'))

      await dragCardTo(wrapper, 'Alpha', 2, 0)

      expect(apiPatch).toHaveBeenCalledTimes(1)
      expect(apiPatch).toHaveBeenCalledWith('/api/tasks/a', {
        workspace: 'personal',
        expected_revision: REVISION,
        status: 'in_review',
        placement: { before_id: null, after_id: null, expected_order_version: 7 },
      })
      expect(cardTitles(wrapper, 2)).toEqual(['Alpha'])

      // And back to a populated lane that sits off the board's scrolled edge.
      apiPatch.mockResolvedValueOnce(placed('a', { ...SEQUENCES, backlog: ['a', 'b', 'c'] }, 'backlog', 9))
      await dragCardTo(wrapper, 'Alpha', 0, 1)
      expect(apiPatch).toHaveBeenLastCalledWith('/api/tasks/a', expect.objectContaining({
        status: 'backlog',
        placement: { before_id: 'c', after_id: null, expected_order_version: 8 },
      }))
      wrapper.unmount()
    })

    it('cross-lane move commits status and placement in one PATCH', async () => {
      const wrapper = await mountPlaced()
      apiPatch.mockResolvedValueOnce(placed('a', { ...SEQUENCES, backlog: ['b', 'c'], in_progress: ['d', 'a', 'e'] }, 'in_progress'))

      await dragCardTo(wrapper, 'Alpha', 1, 1)

      expect(apiPatch).toHaveBeenCalledTimes(1)
      expect(apiPatch).toHaveBeenCalledWith('/api/tasks/a', {
        workspace: 'personal',
        expected_revision: REVISION,
        status: 'in_progress',
        placement: { before_id: 'e', after_id: null, expected_order_version: 7 },
      })
      expect(apiPost).not.toHaveBeenCalled()
      expect(cardTitles(wrapper, 1)).toEqual(['Delta', 'Alpha', 'Echo'])
      wrapper.unmount()
    })

    it('status-filtered single lane still reorders', async () => {
      const wrapper = await mountPlaced()
      await wrapper.findAll('.task-chip').find((c) => c.text().startsWith('To do'))!.trigger('click')
      await nextTick()
      expect(lanes(wrapper)).toHaveLength(1)
      apiPost.mockResolvedValueOnce(placed('c', { ...SEQUENCES, backlog: ['a', 'c', 'b'] }))

      await dragCardTo(wrapper, 'Charlie', 0, 1)

      expect(apiPost).toHaveBeenCalledWith('/api/tasks/c/order', {
        workspace: 'personal', before_id: 'b', after_id: null, expected_order_version: 7,
      })
      expect(cardTitles(wrapper, 0)).toEqual(['Alpha', 'Charlie', 'Bravo'])
      wrapper.unmount()
    })

    it('dropping into the open Done lane at a position holds placement until confirmation and sends it with complete', async () => {
      const wrapper = await mountPlaced()
      apiPost.mockResolvedValueOnce(placed('a', { ...SEQUENCES, backlog: ['b', 'c'], done: ['g', 'a', 'h'] }, 'done'))

      await dragCardTo(wrapper, 'Alpha', 3, 1)

      // Nothing is written, and the card has not moved, until the sheet is confirmed.
      expect(apiPost).not.toHaveBeenCalled()
      expect(apiPatch).not.toHaveBeenCalled()
      expect(wrapper.get('.task-resolution-sheet').text()).toContain('Complete with resolution')
      expect(cardTitles(wrapper, 0)).toContain('Alpha')
      expect(liveText(wrapper)).toContain('Confirm to mark Alpha done')

      await wrapper.get('.task-resolution-sheet form').trigger('submit')
      await flushPromises()
      expect(apiPost).toHaveBeenCalledTimes(1)
      expect(apiPost).toHaveBeenCalledWith('/api/tasks/a/complete', {
        workspace: 'personal',
        expected_revision: REVISION,
        resolution: '',
        placement: { before_id: 'h', after_id: null, expected_order_version: 7 },
      })
      expect(apiPatch).not.toHaveBeenCalled()
      expect(cardTitles(wrapper, 3)).toEqual(['Golf', 'Alpha', 'Hotel'])
      wrapper.unmount()
    })

    it('dropping into the collapsed Done tab appends on completion', async () => {
      localStorage.removeItem('ciao.tasks.doneShown')
      const wrapper = await mountPlaced()
      apiPost.mockResolvedValueOnce(placed('a', { ...SEQUENCES, backlog: ['b', 'c'], done: ['g', 'h', 'a'] }, 'done'))
      await pickUp(wrapper, 'Alpha')
      const x = lanes(wrapper).length * LANE_STEP + 10
      await dragTo(x, 60)
      await release(x, 60)
      expect(apiPost).not.toHaveBeenCalled()

      await wrapper.get('.task-resolution-sheet form').trigger('submit')
      await flushPromises()
      // No placement rides along, so the server appends.
      expect(apiPost).toHaveBeenCalledWith('/api/tasks/a/complete', {
        workspace: 'personal', expected_revision: REVISION, resolution: '',
      })
      wrapper.unmount()
    })

    it('cancelling the resolution sheet leaves status and order unchanged', async () => {
      const wrapper = await mountPlaced()
      await dragCardTo(wrapper, 'Alpha', 3, 1)
      const cancel = wrapper.get('.task-resolution-sheet').findAll('button').find((b) => b.text() === 'Cancel')!
      await cancel.trigger('click')
      await nextTick()

      expect(wrapper.find('.task-resolution-sheet').exists()).toBe(false)
      expect(apiPost).not.toHaveBeenCalled()
      expect(apiPatch).not.toHaveBeenCalled()
      expect(cardTitles(wrapper, 0)).toEqual(['Alpha', 'Bravo', 'Charlie'])
      expect(cardTitles(wrapper, 3)).toEqual(['Golf', 'Hotel'])

      // The held placement is gone: completing from the checkbox appends.
      apiPost.mockResolvedValueOnce(placed('a', { ...SEQUENCES, backlog: ['b', 'c'], done: ['g', 'h', 'a'] }, 'done'))
      await card(wrapper, 'Alpha').get('button[aria-label="Mark Alpha done"]').trigger('click')
      await wrapper.get('.task-resolution-sheet form').trigger('submit')
      await flushPromises()
      expect(apiPost).toHaveBeenCalledWith('/api/tasks/a/complete', {
        workspace: 'personal', expected_revision: REVISION, resolution: '',
      })
      wrapper.unmount()
    })

    it('Escape and pointercancel cancel with no request', async () => {
      const wrapper = await mountPlaced()
      await pickUp(wrapper, 'Alpha')
      const at = dropPoint(1, 1)
      await dragTo(at.x, at.y)
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true }))
      await flushPromises()
      expect(document.querySelector('.drag-overlay')).toBeNull()
      expect(wrapper.find('.drag-placeholder').exists()).toBe(false)
      expect(liveText(wrapper)).toContain('Move cancelled')

      await pickUp(wrapper, 'Alpha')
      await dragTo(at.x, at.y)
      boardRoot().dispatchEvent(new Event('pointercancel'))
      await flushPromises()
      expect(document.querySelector('.drag-overlay')).toBeNull()
      expect(apiPost).not.toHaveBeenCalled()
      expect(apiPatch).not.toHaveBeenCalled()
      expect(cardTitles(wrapper, 0)).toEqual(['Alpha', 'Bravo', 'Charlie'])
      wrapper.unmount()
    })

    it('a completed drag does not open the card', async () => {
      const wrapper = await mountPlaced()
      apiPost.mockResolvedValueOnce(placed('c', { ...SEQUENCES, backlog: ['c', 'a', 'b'] }))
      await pickUp(wrapper, 'Charlie')
      const at = dropPoint(0, 0)
      await dragTo(at.x, at.y)
      boardRoot().dispatchEvent(pointer('pointerup', at.x, at.y))
      // The browser's click after the release lands on the card; the engine eats it.
      card(wrapper, 'Charlie').element.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }))
      await flushPromises()
      expect(wrapper.find('.task-sheet').exists()).toBe(false)

      // A plain click afterwards still opens it.
      await new Promise((resolve) => setTimeout(resolve, 5))
      await card(wrapper, 'Charlie').trigger('click')
      await flushPromises()
      expect(wrapper.find('.task-sheet').exists()).toBe(true)
      wrapper.unmount()
    })

    it('reorders from the card surface, not only from its grip', async () => {
      const wrapper = await mountPlaced()
      apiPost.mockResolvedValueOnce(placed('c', { ...SEQUENCES, backlog: ['c', 'a', 'b'] }))

      await pickUpSurface(wrapper, 'Charlie')
      expect(document.querySelector('.drag-overlay')).not.toBeNull()
      const at = dropPoint(0, 0)
      await dragTo(at.x, at.y)
      await release(at.x, at.y)

      expect(apiPost).toHaveBeenCalledTimes(1)
      expect(apiPost).toHaveBeenCalledWith('/api/tasks/c/order', {
        workspace: 'personal', before_id: 'a', after_id: null, expected_order_version: 7,
      })
      expect(cardTitles(wrapper, 0)).toEqual(['Charlie', 'Alpha', 'Bravo'])
      wrapper.unmount()
    })

    it('a short press on the card surface opens the editor rather than dragging', async () => {
      const wrapper = await mountPlaced()
      layOutBoard()
      const at = card(wrapper, 'Alpha').element.getBoundingClientRect()
      const text = card(wrapper, 'Alpha').get('.task-title').element
      text.dispatchEvent(pointer('pointerdown', at.left + 20, at.top + 10))
      text.dispatchEvent(pointer('pointerup', at.left + 20, at.top + 12))
      await flushPromises()
      await nextTick()

      expect(document.querySelector('.drag-overlay')).toBeNull()
      expect(apiPost).not.toHaveBeenCalled()
      await card(wrapper, 'Alpha').trigger('click')
      await flushPromises()
      expect(wrapper.find('.task-sheet').exists()).toBe(true)
      wrapper.unmount()
    })

    it('accepts a drop far below the last card of a short lane', async () => {
      const wrapper = await mountPlaced()
      apiPatch.mockResolvedValueOnce(placed(
        'a', { ...SEQUENCES, backlog: ['b', 'c'], in_progress: ['d', 'e', 'a'] }, 'in_progress',
      ))

      await pickUpSurface(wrapper, 'Alpha')
      // In progress holds two cards; the release is well under the last one,
      // where a short lane's empty space is.
      const box = lanes(wrapper)[1]!.element.getBoundingClientRect()
      await dragTo(box.left + 100, box.top + 900)
      await release(box.left + 100, box.top + 900)

      expect(apiPatch).toHaveBeenCalledWith('/api/tasks/a', {
        workspace: 'personal',
        expected_revision: REVISION,
        status: 'in_progress',
        placement: { before_id: null, after_id: 'e', expected_order_version: 7 },
      })
      wrapper.unmount()
    })

    it('leaves the card checkbox and menu working when the card surface can drag', async () => {
      const wrapper = await mountPlaced()
      layOutBoard()
      const alpha = card(wrapper, 'Alpha')
      const at = alpha.element.getBoundingClientRect()
      const pressFar = (el: Element) => {
        el.dispatchEvent(pointer('pointerdown', at.left + 20, at.top + 10))
        boardRoot().dispatchEvent(pointer('pointermove', at.left + 20, at.top + 40))
        boardRoot().dispatchEvent(pointer('pointerup', at.left + 20, at.top + 40))
      }

      // The menu: a press on its trigger must not become a card drag.
      pressFar(alpha.get('.task-more').element)
      await flushPromises()
      expect(document.querySelector('.drag-overlay')).toBeNull()
      const items = await openMenu(wrapper, 'Alpha')
      expect(items.map((el) => el.textContent)).toEqual(['Move up', 'Move down'])
      await closeAnyMenu()

      // The Done checkbox: same guard, and its own click still completes.
      pressFar(alpha.get('.task-check').element)
      await flushPromises()
      expect(document.querySelector('.drag-overlay')).toBeNull()
      await card(wrapper, 'Alpha').get('.task-check').trigger('click')
      await nextTick()
      expect(wrapper.find('.task-resolution-sheet').exists()).toBe(true)
      wrapper.unmount()
    })

    it('opens a link in a title instead of starting a drag from it', async () => {
      const wrapper = await mountPlaced({}, [
        ...PLACED,
        task({ id: 'u', title: 'Read https://example.com/doc', status: 'backlog' }),
      ])
      layOutBoard()
      const urlCard = card(wrapper, 'example.com')
      const at = urlCard.element.getBoundingClientRect()
      const link = urlCard.get('.task-title a').element
      link.dispatchEvent(pointer('pointerdown', at.left + 20, at.top + 10))
      boardRoot().dispatchEvent(pointer('pointermove', at.left + 20, at.top + 40))
      await nextTick()
      expect(document.querySelector('.drag-overlay')).toBeNull()
      expect(boardRoot().style.getPropertyValue('--drag-item-height')).toBe('')

      await urlCard.get('.task-title a').trigger('click')
      await nextTick()
      expect(wrapper.find('.task-sheet').exists()).toBe(false)
      wrapper.unmount()
    })

    it('Move up and Move down menu items work and are disabled at the ends', async () => {
      const wrapper = await mountPlaced()
      let items = await openMenu(wrapper, 'Alpha')
      expect(items.map((el) => el.textContent)).toEqual(['Move up', 'Move down'])
      expect(byLabel(items, 'Move up').getAttribute('aria-disabled')).toBe('true')
      expect(byLabel(items, 'Move down').getAttribute('aria-disabled')).not.toBe('true')
      await closeAnyMenu()

      items = await openMenu(wrapper, 'Charlie')
      expect(byLabel(items, 'Move down').getAttribute('aria-disabled')).toBe('true')
      await closeAnyMenu()

      apiPost.mockResolvedValueOnce(placed('b', { ...SEQUENCES, backlog: ['b', 'a', 'c'] }))
      items = await openMenu(wrapper, 'Bravo')
      byLabel(items, 'Move up').click()
      await flushPromises()
      await nextTick()
      expect(apiPost).toHaveBeenCalledTimes(1)
      expect(apiPost).toHaveBeenCalledWith('/api/tasks/b/order', {
        workspace: 'personal', before_id: 'a', after_id: null, expected_order_version: 7,
      })
      expect(cardTitles(wrapper, 0)).toEqual(['Bravo', 'Alpha', 'Charlie'])
      expect(liveText(wrapper)).toContain('Dropped Bravo at position 1 of 3 in To do.')

      // Move down from the top of [Bravo, Alpha, Charlie] lands before Charlie.
      apiPost.mockResolvedValueOnce(placed('b', { ...SEQUENCES, backlog: ['a', 'b', 'c'] }, 'backlog', 9))
      items = await openMenu(wrapper, 'Bravo')
      byLabel(items, 'Move down').click()
      await flushPromises()
      expect(apiPost).toHaveBeenLastCalledWith('/api/tasks/b/order', {
        workspace: 'personal', before_id: 'c', after_id: null, expected_order_version: 8,
      })
      wrapper.unmount()
    })

    it('a deferred save failure does not take focus back from the card the user moved to', async () => {
      const wrapper = await mountPlaced()
      let fail!: (error: Error) => void
      apiPost.mockReturnValueOnce(new Promise((_resolve, reject) => { fail = reject }))
      const handle = grip(wrapper, 'Alpha')
      ;(handle.element as HTMLElement).focus()
      await handle.trigger('keydown', key(' '))
      await nextTick()
      await grip(wrapper, 'Alpha').trigger('keydown', key('ArrowDown'))
      await nextTick()
      await grip(wrapper, 'Alpha').trigger('keydown', key('Enter'))

      // While the write is in flight the user moves to another card.
      const other = card(wrapper, 'Bravo').get('.task-open').element as HTMLElement
      other.focus()
      expect(document.activeElement).toBe(other)

      fail(new Error('disk full'))
      await flushPromises()
      await nextTick()

      expect(liveText(wrapper)).toBe('Could not save the new order.')
      expect(document.activeElement).toBe(other)
      wrapper.unmount()
    })

    it('a cancelled deferred failure does not restore focus to its card', async () => {
      const { createMemoryHistory, createRouter } = await import('vue-router')
      const router = createRouter({
        history: createMemoryHistory(),
        routes: [{ path: '/:pathMatch(.*)*', component: { template: '<div />' } }],
      })
      await router.push('/tasks')
      await router.isReady()
      apiGet.mockImplementation((url: string) =>
        Promise.resolve(
          url.includes('/api/tasks?')
            ? { workspace: 'personal', tasks: PLACED, order: orderPayload() }
            : {},
        ),
      )
      const wrapper = mount(TaskBoardView, { attachTo: document.body, global: { plugins: [router] } })
      await flushPromises()
      await nextTick()
      layOutBoard()

      let fail!: (error: Error) => void
      apiPost.mockReturnValueOnce(new Promise((_resolve, reject) => { fail = reject }))
      const handle = grip(wrapper, 'Alpha')
      ;(handle.element as HTMLElement).focus()
      await handle.trigger('keydown', key(' '))
      await nextTick()
      await grip(wrapper, 'Alpha').trigger('keydown', key('ArrowDown'))
      await nextTick()
      await grip(wrapper, 'Alpha').trigger('keydown', key('Enter'))

      // Navigation cancels the drag while the write is still in flight.
      await router.push('/elsewhere')
      await nextTick()
      expect(wrapper.find('.drag-lifted').exists()).toBe(false)
      ;(document.activeElement as HTMLElement).blur()
      expect(document.activeElement).toBe(document.body)

      fail(new Error('disk full'))
      await flushPromises()
      await nextTick()
      expect(document.activeElement).toBe(document.body)
      wrapper.unmount()
    })

    it('a deferred pointer failure restores the grip after its focused row remounts', async () => {
      const wrapper = await mountPlaced()
      let fail!: (error: Error) => void
      apiPost.mockReturnValueOnce(new Promise((_resolve, reject) => { fail = reject }))

      const handle = grip(wrapper, 'Alpha').element as HTMLElement
      handle.focus()
      await pickUp(wrapper, 'Alpha')
      const at = dropPoint(0, 1)
      await dragTo(at.x, at.y)
      expect(wrapper.findAll('.drag-placeholder')).toHaveLength(1)
      expect(document.activeElement).toBe(handle)

      // The placeholder re-render replaces the focused grip and focus falls to
      // the document.
      const fresh = handle.cloneNode(true) as HTMLElement
      handle.replaceWith(fresh)
      expect(handle.isConnected).toBe(false)
      expect(document.activeElement).toBe(document.body)

      boardRoot().dispatchEvent(pointer('pointerup', at.x, at.y))
      await flushPromises()
      fail(new Error('disk full'))
      await flushPromises()
      await nextTick()

      expect(document.activeElement).toBe(grip(wrapper, 'Alpha').element)
      expect(document.activeElement).toBe(fresh)
      wrapper.unmount()
    })

    it('a deferred pointer failure does not restore the grip after navigation', async () => {
      const { createMemoryHistory, createRouter } = await import('vue-router')
      const router = createRouter({
        history: createMemoryHistory(),
        routes: [{ path: '/:pathMatch(.*)*', component: { template: '<div />' } }],
      })
      await router.push('/tasks')
      await router.isReady()
      apiGet.mockImplementation((url: string) =>
        Promise.resolve(
          url.includes('/api/tasks?')
            ? { workspace: 'personal', tasks: PLACED, order: orderPayload() }
            : {},
        ),
      )
      const wrapper = mount(TaskBoardView, { attachTo: document.body, global: { plugins: [router] } })
      await flushPromises()
      await nextTick()
      layOutBoard()

      let fail!: (error: Error) => void
      apiPost.mockReturnValueOnce(new Promise((_resolve, reject) => { fail = reject }))
      const handle = grip(wrapper, 'Alpha').element as HTMLElement
      handle.focus()
      await pickUp(wrapper, 'Alpha')
      const at = dropPoint(0, 1)
      await dragTo(at.x, at.y)
      expect(wrapper.findAll('.drag-placeholder')).toHaveLength(1)
      const fresh = handle.cloneNode(true) as HTMLElement
      handle.replaceWith(fresh)
      expect(handle.isConnected).toBe(false)
      expect(document.activeElement).toBe(document.body)

      boardRoot().dispatchEvent(pointer('pointerup', at.x, at.y))
      await flushPromises()
      await router.push('/elsewhere')
      await nextTick()

      fail(new Error('disk full'))
      await flushPromises()
      await nextTick()
      expect(document.activeElement).toBe(document.body)
      expect(document.activeElement).not.toBe(fresh)
      wrapper.unmount()
    })

    it('a deferred pointer failure does not restore the grip after focus moved away', async () => {
      const wrapper = await mountPlaced()
      let fail!: (error: Error) => void
      apiPost.mockReturnValueOnce(new Promise((_resolve, reject) => { fail = reject }))

      await pickUp(wrapper, 'Charlie')
      const at = dropPoint(0, 0)
      await dragTo(at.x, at.y)
      boardRoot().dispatchEvent(pointer('pointerup', at.x, at.y))
      await flushPromises()
      ;(document.activeElement as HTMLElement).blur()
      expect(document.activeElement).toBe(document.body)

      fail(new Error('disk full'))
      await flushPromises()
      await nextTick()
      expect(document.activeElement).toBe(document.body)
      wrapper.unmount()
    })

    it('a deferred menu failure does not restore the grip after focus moved away', async () => {
      const wrapper = await mountPlaced()
      let fail!: (error: Error) => void
      apiPost.mockReturnValueOnce(new Promise((_resolve, reject) => { fail = reject }))

      const items = await openMenu(wrapper, 'Bravo')
      byLabel(items, 'Move up').click()
      await flushPromises()
      // Menu close owns restoration to its trigger; the engine must not move
      // that focus to the grip when the deferred write fails.
      await new Promise((resolve) => setTimeout(resolve, 0))
      const trigger = card(wrapper, 'Bravo').get('.task-more').element as HTMLElement
      expect(document.activeElement).toBe(trigger)

      fail(new Error('disk full'))
      await flushPromises()
      await nextTick()
      expect(liveText(wrapper)).toBe('Could not save the new order.')
      expect(document.activeElement).toBe(trigger)
      expect(document.activeElement).not.toBe(grip(wrapper, 'Bravo').element)
      wrapper.unmount()
    })

    it('save failure shows the error, reloads and keeps focus on the handle', async () => {
      const wrapper = await mountPlaced()
      apiPost.mockRejectedValueOnce(new Error('disk full'))
      const reads = boardReads()
      // A real pointer press focuses the grip; the failed write must leave that
      // focus where it is.
      ;(grip(wrapper, 'Charlie').element as HTMLElement).focus()

      await dragCardTo(wrapper, 'Charlie', 0, 0)
      await nextTick()

      expect(wrapper.get('.task-action-error').text()).toContain('disk full')
      expect(boardReads()).toBe(reads + 1)
      expect(liveText(wrapper)).toBe('Could not save the new order.')
      expect(cardTitles(wrapper, 0)).toEqual(['Alpha', 'Bravo', 'Charlie'])
      expect(document.activeElement).toBe(grip(wrapper, 'Charlie').element)
      wrapper.unmount()
    })

    it('409 order conflict refreshes and explains, does not replay', async () => {
      const wrapper = await mountPlaced()
      apiPost.mockRejectedValueOnce(conflict('task_order_conflict'))
      const reads = boardReads()

      await dragCardTo(wrapper, 'Charlie', 0, 0)
      await nextTick()

      expect(wrapper.get('.task-action-error').text()).toContain('The board changed while you were moving that')
      expect(boardReads()).toBe(reads + 1)
      expect(liveText(wrapper)).toContain('Could not save the new order. The board changed')
      expect(apiPost).toHaveBeenCalledTimes(1)
      expect(apiPatch).not.toHaveBeenCalled()
      wrapper.unmount()
    })

    it('a tasks_changed re-read during a drag cancels it with a polite message', async () => {
      const wrapper = await mountPlaced()
      await pickUp(wrapper, 'Alpha')
      const at = dropPoint(1, 1)
      await dragTo(at.x, at.y)
      expect(document.querySelector('.drag-overlay')).not.toBeNull()

      const signals = useTaskSignalsStore()
      signals.loadedWorkspace = 'personal'
      signals.tasks = [...PLACED] as Task[]
      await flushPromises()
      await nextTick()

      expect(document.querySelector('.drag-overlay')).toBeNull()
      expect(wrapper.find('.drag-placeholder').exists()).toBe(false)
      expect(liveText(wrapper)).toBe('The board changed, so your move was cancelled.')
      await release(at.x, at.y)
      expect(apiPatch).not.toHaveBeenCalled()
      expect(apiPost).not.toHaveBeenCalled()
      wrapper.unmount()
    })

    it('keyboard pickup, Arrow, drop commits; Shift+Arrow still moves one lane', async () => {
      const wrapper = await mountPlaced()
      apiPost.mockResolvedValueOnce(placed('a', { ...SEQUENCES, backlog: ['b', 'a', 'c'] }))
      const handle = grip(wrapper, 'Alpha')
      ;(handle.element as HTMLElement).focus()
      await handle.trigger('keydown', key(' '))
      await nextTick()
      // The row stays visible and is marked picked up; no overlay, no placeholder.
      expect(card(wrapper, 'Alpha').classes()).toContain('drag-lifted')
      expect(document.querySelector('.drag-overlay')).toBeNull()
      await grip(wrapper, 'Alpha').trigger('keydown', key('ArrowDown'))
      await nextTick()
      expect(cardTitles(wrapper, 0)).toEqual(['Bravo', 'Alpha', 'Charlie'])
      expect(apiPost).not.toHaveBeenCalled()
      await grip(wrapper, 'Alpha').trigger('keydown', key('Enter'))
      await flushPromises()
      await nextTick()
      expect(apiPost).toHaveBeenCalledTimes(1)
      expect(apiPost).toHaveBeenCalledWith('/api/tasks/a/order', {
        workspace: 'personal', before_id: 'c', after_id: null, expected_order_version: 7,
      })

      // Shift+Arrow on the title is the old one-lane move, with no placement.
      apiPatch.mockResolvedValueOnce(placed('b', { ...SEQUENCES, backlog: ['a', 'c'], in_progress: ['d', 'e', 'b'] }, 'in_progress', 9))
      await card(wrapper, 'Bravo').get('.task-open').trigger('keydown', key('ArrowRight', { shiftKey: true }))
      await flushPromises()
      expect(apiPatch).toHaveBeenCalledTimes(1)
      expect(apiPatch).toHaveBeenCalledWith('/api/tasks/b', {
        workspace: 'personal', expected_revision: REVISION, status: 'in_progress',
      })
      wrapper.unmount()
    })

    it('reordering a done card does not call complete', async () => {
      const wrapper = await mountPlaced()
      apiPost.mockResolvedValueOnce(placed('h', { ...SEQUENCES, done: ['h', 'g'] }, 'done'))

      await dragCardTo(wrapper, 'Hotel', 3, 0)

      expect(apiPost).toHaveBeenCalledTimes(1)
      expect(apiPost).toHaveBeenCalledWith('/api/tasks/h/order', {
        workspace: 'personal', before_id: 'g', after_id: null, expected_order_version: 7,
      })
      expect(apiPost.mock.calls.some((call) => String(call[0]).endsWith('/complete'))).toBe(false)
      expect(wrapper.find('.task-resolution-sheet').exists()).toBe(false)
      expect(cardTitles(wrapper, 3)).toEqual(['Hotel', 'Golf'])
      wrapper.unmount()
    })

    it('live region announces pickup, move, drop, cancel, failure', async () => {
      const wrapper = await mountPlaced()
      apiPost.mockResolvedValueOnce(placed('a', { ...SEQUENCES, backlog: ['b', 'a', 'c'] }))
      expect(wrapper.find('[role="status"][aria-live="polite"]').exists()).toBe(true)

      await grip(wrapper, 'Alpha').trigger('keydown', key(' '))
      await nextTick()
      expect(liveText(wrapper)).toBe('Picked up Alpha, position 1 of 3 in To do.')
      await grip(wrapper, 'Alpha').trigger('keydown', key('ArrowDown'))
      expect(liveText(wrapper)).toBe('Alpha, position 2 of 3 in To do.')
      await grip(wrapper, 'Alpha').trigger('keydown', key('Enter'))
      await flushPromises()
      expect(liveText(wrapper)).toBe('Dropped Alpha at position 2 of 3 in To do.')

      await grip(wrapper, 'Alpha').trigger('keydown', key(' '))
      await nextTick()
      await grip(wrapper, 'Alpha').trigger('keydown', key('Escape'))
      await flushPromises()
      expect(liveText(wrapper)).toBe('Move cancelled. Alpha is back at position 2 of 3 in To do.')

      apiPost.mockRejectedValueOnce(new Error('disk full'))
      await grip(wrapper, 'Alpha').trigger('keydown', key(' '))
      await nextTick()
      await grip(wrapper, 'Alpha').trigger('keydown', key('ArrowUp'))
      await grip(wrapper, 'Alpha').trigger('keydown', key('Enter'))
      await flushPromises()
      expect(liveText(wrapper)).toBe('Could not save the new order.')
      wrapper.unmount()
    })

    it('shows an unreadable order file as a notice, disables placement, and keeps status moves', async () => {
      const wrapper = await mountPlaced({ order_error: 'Could not read Workspace/task-order.json' })
      const notice = wrapper.get('.task-order-notice')
      expect(notice.text()).toBe('Could not read Workspace/task-order.json')
      expect(notice.classes()).toContain('task-action-error')

      const handle = grip(wrapper, 'Alpha')
      expect(handle.attributes('disabled')).toBeDefined()
      expect(handle.attributes('title')).toBe('Could not read Workspace/task-order.json')
      await pickUp(wrapper, 'Alpha')
      expect(document.querySelector('.drag-overlay')).toBeNull()

      const items = await openMenu(wrapper, 'Bravo')
      expect(byLabel(items, 'Move up').getAttribute('aria-disabled')).toBe('true')
      expect(byLabel(items, 'Move down').getAttribute('aria-disabled')).toBe('true')
      expect(byLabel(items, 'Move up').getAttribute('title')).toBe('Could not read Workspace/task-order.json')
      await closeAnyMenu()

      // Status changes still work: Shift+Arrow writes the status alone.
      apiPatch.mockResolvedValueOnce(placed('a', SEQUENCES, 'in_progress'))
      await card(wrapper, 'Alpha').get('.task-open').trigger('keydown', key('ArrowRight', { shiftKey: true }))
      await flushPromises()
      expect(apiPatch).toHaveBeenCalledWith('/api/tasks/a', {
        workspace: 'personal', expected_revision: REVISION, status: 'in_progress',
      })
      expect(apiPost).not.toHaveBeenCalled()
      wrapper.unmount()
    })
  })
})

/** The pane's Retry path: a reload against the same workspace. */
async function useTaskBoardReload(wrapper: ReturnType<typeof mount>) {
  const { useTaskBoardStore } = await import('../../stores/taskBoard')
  await useTaskBoardStore().reload('personal')
  return wrapper
}
