// @vitest-environment jsdom

/**
 * The task board's pure half.
 *
 * These are the decisions both the store and the pane make and neither may make
 * alone: which rows are tasks, which lane a row belongs in, what order a column
 * draws, when a due date counts as overdue, and — the one that carries the whole
 * board's honesty — what sentence a refusal shows.
 */
import { describe, expect, it } from 'vitest'
import {
  TASK_COLUMNS,
  TASK_DUE_FILTERS,
  TASK_NO_PROJECT,
  formatTaskDue,
  invalidTaskRows,
  isOverdue,
  isTaskInvalidRow,
  localDateKey,
  matchesDueFilter,
  matchesProjectFilter,
  openTaskCount,
  readableTasks,
  sortTasks,
  statusCounts,
  taskApiErrorMessage,
  taskDetailFrom,
  doneToday,
  taskLanes,
  titleSegments,
  splitTaskLog,
  joinTaskLog,
  taskReconcileBadgeClass,
  taskReconcileLabel,
  taskReconcileNotes,
  taskRowsFrom,
  toTaskListRow,
} from './taskBoard'
import type { Task } from './types'

/** The day the fixtures' `updated_at` falls on, so Done keeps them. */
const FIXTURE_DAY = new Date('2026-03-01T12:00:00Z')

function task(overrides: Partial<Task> = {}): Task {
  return {
    id: 'a',
    title: 'A task',
    status: 'backlog',
    project_id: '',
    due: '',
    assignee: 'user',
    chat_id: '',
    attempt_id: '',
    created_at: '2026-03-01T09:00:00+00:00',
    updated_at: '2026-03-01T09:00:00+00:00',
    // A hex string, and it has to stay one: the store sends this exact value
    // back as `expected_revision`, and a number here would be a revision the
    // server never issued.
    revision: 'a'.repeat(64),
    relative_path: 'Tasks/a.md',
    // The delegation facts. Empty by default, which is what "never delegated"
    // looks like; each test that exercises one sets it explicitly.
    attempt_state: '',
    attempt_outcome: '',
    attempt_summary: '',
    attempt_detail: '',
    live_attempt_id: '',
    changed_since_delegated: false,
    // A done fixture is finished today, as the board's own fixtures say; a test that
    // needs an undated done task passes `completed_at: null` explicitly.
    completed_at: overrides.status === 'done' ? '2026-03-01T08:00:00Z' : null,
    has_resolution: false,
    ...overrides,
  }
}

describe('taskRowsFrom', () => {
  it('reads a list row with every field the board needs', () => {
    const rows = taskRowsFrom({
      workspace: 'personal',
      tasks: [task({ id: 'ship', title: 'Ship it', status: 'in_progress', due: '2026-03-20' })],
    })
    expect(rows).toHaveLength(1)
    expect(rows[0]).toMatchObject({
      id: 'ship',
      title: 'Ship it',
      status: 'in_progress',
      due: '2026-03-20',
      revision: 'a'.repeat(64),
    })
  })

  it('keeps a malformed file as a row of its own rather than dropping it', () => {
    const rows = taskRowsFrom({
      tasks: [
        task(),
        { id: 'broken', path: 'Tasks/broken.md', code: 'invalid_task', message: 'no title' },
      ],
    })
    expect(rows).toHaveLength(2)
    expect(readableTasks(rows).map((t) => t.id)).toEqual(['a'])
    expect(invalidTaskRows(rows)).toEqual([
      { id: 'broken', path: 'Tasks/broken.md', code: 'invalid_task', message: 'no title' },
    ])
    expect(isTaskInvalidRow(rows[1])).toBe(true)
    expect(isTaskInvalidRow(rows[0])).toBe(false)
  })

  it('defaults every field, so an older or shorter payload cannot blank a column', () => {
    const rows = taskRowsFrom({ tasks: [{}, { status: 'nonsense' }] })
    expect(rows[0]).toMatchObject({
      id: '',
      title: '',
      status: 'backlog',
      assignee: 'user',
      revision: '',
    })
    // A status this build does not know is not a fifth column.
    expect(rows[1]).toMatchObject({ status: 'backlog' })
  })

  it('reads a payload with no task list as an empty board, not a throw', () => {
    expect(taskRowsFrom(null)).toEqual([])
    expect(taskRowsFrom({})).toEqual([])
    expect(taskRowsFrom({ tasks: 'nope' })).toEqual([])
  })
})

describe('toTaskListRow', () => {
  it('drops the body a write answers with and the list never carries', () => {
    const row = toTaskListRow({ ...task(), body: '# Notes\n\nprose' })
    expect('body' in row).toBe(false)
    expect(row.id).toBe('a')
  })
})

describe('taskDetailFrom', () => {
  it('reads the record a get or a write answers with, body included', () => {
    const detail = taskDetailFrom({ ...task({ id: 'ship' }), body: '# Notes' })
    expect(detail).toMatchObject({ id: 'ship', title: 'A task', revision: 'a'.repeat(64), body: '# Notes' })
  })

  it('reads a 200 that is not a task as an empty one rather than undefined fields', () => {
    // A write's answer is adopted straight onto the board, and the editor writes
    // these fields back, so a payload without them has to normalise to strings.
    // A `.trim()` on `undefined` is what a stubbed or drifted backend turns into
    // a blank pane — which is exactly what a browser pass caught.
    for (const payload of [undefined, null, {}]) {
      const detail = taskDetailFrom(payload)
      expect(detail.id).toBe('')
      expect(detail.title).toBe('')
      expect(detail.status).toBe('backlog')
      expect(detail.revision).toBe('')
      expect(detail.body).toBe('')
      expect(() => detail.title.trim()).not.toThrow()
    }
  })

  it('does not read an unreadable-file row as a task with a title', () => {
    const detail = taskDetailFrom({ id: 'broken', path: 'Tasks/broken.md', code: 'invalid_task', message: 'no title' })
    expect(detail.title).toBe('')
    expect(detail.id).toBe('')
  })
})

describe('taskLanes', () => {
  const rows = [
    task({ id: 'b1', title: 'Backlog one', status: 'backlog' }),
    task({ id: 'i1', title: 'Doing', status: 'in_progress' }),
    task({ id: 'h1', title: 'Waiting', status: 'in_review' }),
    task({ id: 'd1', title: 'Shipped', status: 'done' }),
    task({ id: 'b2', title: 'Backlog two', status: 'backlog' }),
  ]

  it('gives a wide unfiltered board the four fixed columns, in board order', () => {
    const lanes = taskLanes(rows, { status: 'all', now: FIXTURE_DAY })
    expect(lanes.map((lane) => lane.label)).toEqual(['To do', 'In progress', 'In review', 'Done'])
    expect(lanes.map((lane) => lane.tasks.map((t) => t.id))).toEqual([
      ['b1', 'b2'],
      ['i1'],
      ['h1'],
      ['d1'],
    ])
  })

  it('narrows to one lane on either layout when a status is picked', () => {
    const lanes = taskLanes(rows, { status: 'in_review', now: FIXTURE_DAY })
    expect(lanes).toHaveLength(1)
    expect(lanes[0]!.label).toBe('In review')
    expect(lanes[0]!.tasks.map((t) => t.id)).toEqual(['h1'])
  })

  it('shows an empty lane rather than dropping a column', () => {
    const lanes = taskLanes([task({ status: 'done' })], { status: 'all', now: FIXTURE_DAY })
    expect(lanes).toHaveLength(4)
    expect(lanes[0]!.tasks).toEqual([])
    expect(lanes[3]!.tasks).toHaveLength(1)
  })

  it('has four columns and no more, so a status this build does not know has no lane', () => {
    expect(TASK_COLUMNS.map((c) => c.status)).toEqual(['backlog', 'in_progress', 'in_review', 'done'])
  })
})

describe('sortTasks', () => {
  it('leads with the dated work, by date, and sinks the undated below it', () => {
    const sorted = sortTasks([
      task({ id: 'late', title: 'Late', due: '2026-04-01' }),
      task({ id: 'none', title: 'Undated' }),
      task({ id: 'soon', title: 'Soon', due: '2026-03-02' }),
    ])
    expect(sorted.map((t) => t.id)).toEqual(['soon', 'late', 'none'])
  })

  it('breaks ties by title, so the same rows always draw in the same order', () => {
    const sorted = sortTasks([
      task({ id: 'b', title: 'Beta' }),
      task({ id: 'a', title: 'Alpha' }),
    ])
    expect(sorted.map((t) => t.title)).toEqual(['Alpha', 'Beta'])
  })

  it('does not mutate the list it was handed', () => {
    const rows = [task({ id: 'late', due: '2026-04-01' }), task({ id: 'soon', due: '2026-03-02' })]
    const before = rows.map((t) => t.id)
    sortTasks(rows)
    expect(rows.map((t) => t.id)).toEqual(before)
  })
})

describe('counts', () => {
  it('counts per status and totals the open work', () => {
    const rows = [
      task({ status: 'backlog' }),
      task({ status: 'backlog' }),
      task({ status: 'in_progress' }),
      task({ status: 'done' }),
    ]
    expect(statusCounts(rows)).toEqual({ backlog: 2, in_progress: 1, in_review: 0, done: 1 })
    expect(openTaskCount(rows)).toBe(3)
  })
})

describe('due dates', () => {
  it('reads a due date as the calendar day the user sees', () => {
    // 2026-03-20 parsed as UTC is the 19th west of Greenwich; formatting it as a
    // plain date is what keeps a card from saying the wrong day.
    expect(formatTaskDue('2026-03-20')).toBe(new Date('2026-03-20T00:00:00').toLocaleDateString(undefined, { month: 'short', day: 'numeric' }))
    expect(formatTaskDue('')).toBe('')
    expect(formatTaskDue('soon')).toBe('')
  })

  it('treats only a dated task before today as overdue', () => {
    expect(isOverdue(task({ due: '2026-03-01' }), '2026-03-10')).toBe(true)
    expect(isOverdue(task({ due: '2026-03-10' }), '2026-03-10')).toBe(false)
    expect(isOverdue(task({ due: '' }), '2026-03-10')).toBe(false)
  })

  it('filters by due date, and every mode but "any" keeps the undated out', () => {
    const dated = task({ due: '2026-03-10' })
    const overdue = task({ due: '2026-03-01' })
    const undated = task({ due: '' })
    const week = task({ due: '2026-03-14' })
    const today = '2026-03-10'

    expect(matchesDueFilter(dated, 'any', today)).toBe(true)
    expect(matchesDueFilter(undated, 'any', today)).toBe(true)
    expect(matchesDueFilter(overdue, 'overdue', today)).toBe(true)
    expect(matchesDueFilter(dated, 'overdue', today)).toBe(false)
    expect(matchesDueFilter(dated, 'today', today)).toBe(true)
    expect(matchesDueFilter(week, 'week', today)).toBe(true)
    // The window ends seven days out and does not wrap.
    expect(matchesDueFilter(task({ due: '2026-03-18' }), 'week', today)).toBe(false)
    expect(matchesDueFilter(undated, 'none', today)).toBe(true)
    expect(matchesDueFilter(dated, 'none', today)).toBe(false)
  })

  it('offers five due filters', () => {
    expect(TASK_DUE_FILTERS.map((f) => f.value)).toEqual(['any', 'overdue', 'today', 'week', 'none'])
  })

  it('builds a local today, not a UTC one', () => {
    expect(localDateKey(new Date(2026, 2, 5))).toBe('2026-03-05')
  })
})

describe('matchesProjectFilter', () => {
  it('keeps every task for no filter, and buckets the unfiled ones', () => {
    expect(matchesProjectFilter(task({ project_id: 'p1' }), '')).toBe(true)
    expect(matchesProjectFilter(task({ project_id: '' }), '')).toBe(true)
    expect(matchesProjectFilter(task({ project_id: '' }), TASK_NO_PROJECT)).toBe(true)
    expect(matchesProjectFilter(task({ project_id: 'p1' }), TASK_NO_PROJECT)).toBe(false)
    // The auto-managed General project is the same General.
    expect(matchesProjectFilter(task({ project_id: 'gen' }), TASK_NO_PROJECT, 'gen')).toBe(true)
    expect(matchesProjectFilter(task({ project_id: 'p1' }), TASK_NO_PROJECT, 'gen')).toBe(false)
    expect(matchesProjectFilter(task({ project_id: 'p1' }), 'p1')).toBe(true)
    expect(matchesProjectFilter(task({ project_id: 'p2' }), 'p1')).toBe(false)
  })
})

describe('taskApiErrorMessage', () => {
  it('unwraps the task surface\'s nested error envelope', () => {
    const refusal = Object.assign(new Error('HTTP 409'), {
      payload: { error: { code: 'task_revision_conflict', message: 'that task changed on disk', retryable: true } },
    })
    // The flat reader alone would hand back `[object Object]` here, and a stale
    // revision is the one refusal the user most needs to read.
    expect(taskApiErrorMessage(refusal, 'fallback')).toBe('that task changed on disk')
  })

  it('falls back to the code when the envelope carries no sentence', () => {
    const refusal = Object.assign(new Error('HTTP 400'), {
      payload: { error: { code: 'task_invalid' } },
    })
    expect(taskApiErrorMessage(refusal, 'fallback')).toBe('task_invalid')
  })

  it('still reads a flat error body, the way the rest of the API answers', () => {
    const refusal = Object.assign(new Error('HTTP 400'), { payload: { error: 'title_required' } })
    expect(taskApiErrorMessage(refusal, 'fallback')).toBe('title_required')
  })

  it('falls back to the message, and then to its own sentence', () => {
    expect(taskApiErrorMessage(new Error('network down'), 'fallback')).toBe('network down')
    expect(taskApiErrorMessage({}, 'fallback')).toBe('fallback')
  })
})
describe('taskReconcileNotes', () => {
  it('does not flag a released ready_for_review with no Review badge', () => {
    // What a card looks like immediately after the user approved Done or detached
    // it: the attempt keeps `ready_for_review` as its recorded outcome, the badge
    // is gone and nothing is live. That used to raise a false
    // `result_without_review`.
    const released = task({
      status: 'done',
      assignee: 'agent',
      attempt_state: 'ready_for_review',
      live_attempt_id: '',
    })
    expect(taskReconcileNotes(released)).toEqual([])
  })

  it('still flags a live ready_for_review whose Review badge is missing', () => {
    const live = task({
      status: 'in_progress',
      assignee: 'agent',
      attempt_state: 'ready_for_review',
      live_attempt_id: 'b'.repeat(32),
    })
    const codes = taskReconcileNotes(live).map((n) => n.code)
    expect(codes).toContain('result_without_review')
  })

  it('labels every code in plain words and mutes the resting-state one', () => {
    // The card never prints the raw machine code; the code travels as a data-
    // attribute instead. The settled-attempt note is the normal resting state and
    // must not wear the error colour.
    expect(taskReconcileLabel('settled_attempt_holds_task')).toBe('Out of step')
    expect(taskReconcileLabel('done_while_live')).toBe('Done but running')
    expect(taskReconcileBadgeClass('settled_attempt_holds_task')).toBe('badge--muted')
    expect(taskReconcileBadgeClass('chat_not_visible')).toBe('badge--error')
  })
})

describe('titleSegments', () => {
  it('splits URLs out of a title with a short label and the full href', () => {
    expect(titleSegments('Check https://www.remotion.dev/docs/ai/skills/ before Friday.')).toEqual([
      { text: 'Check ' },
      { text: 'remotion.dev/docs/ai/skills', href: 'https://www.remotion.dev/docs/ai/skills/' },
      { text: ' before Friday.' },
    ])
  })

  it('truncates a long path and leaves a plain title alone', () => {
    const [, link] = titleSegments('See https://huggingface.co/blog/sora-2/what-is-openai-decisions-api-a-practical-guide')
    expect(link!.text.endsWith('…')).toBe(true)
    expect(link!.text.length).toBe(42)
    expect(link!.href).toBe('https://huggingface.co/blog/sora-2/what-is-openai-decisions-api-a-practical-guide')
    expect(titleSegments('Ship it')).toEqual([{ text: 'Ship it' }])
    expect(titleSegments('ftp://nope and javascript:alert(1)')).toEqual([{ text: 'ftp://nope and javascript:alert(1)' }])
  })
})

describe('splitTaskLog / joinTaskLog', () => {
  const log = '<!-- ciao:task-log -->\n## Delegation log\n\n- line <!-- attempt:x -->\n<!-- /ciao:task-log -->'
  it('separates the description from the engine log and puts it back', () => {
    const body = `Do the thing.\n\n${log}\n`
    const { description, log: kept } = splitTaskLog(body)
    expect(description).toBe('Do the thing.\n')
    expect(kept).toBe(log)
    expect(joinTaskLog(description, kept)).toBe(body)
    expect(joinTaskLog('Do it better.\n', kept)).toBe(`Do it better.\n\n${log}\n`)
  })
  it('leaves a body with no log alone', () => {
    expect(splitTaskLog('Plain.\n')).toEqual({ description: 'Plain.\n', log: '' })
    expect(joinTaskLog('Plain.\n', '')).toBe('Plain.\n')
  })
})

describe('Done keeps to today on the unfiltered board', () => {
  it('leaves earlier done tasks to Show all, and counts them', () => {
    const rows = [
      task({ id: 'today', status: 'done', completed_at: '2026-03-01T08:00:00Z' }),
      task({ id: 'old', status: 'done', completed_at: '2026-02-20T08:00:00Z' }),
      task({ id: 'open', status: 'backlog' }),
    ]
    const done = taskLanes(rows, { status: 'all', now: FIXTURE_DAY }).find(l => l.status === 'done')!
    expect(done.tasks.map(t => t.id)).toEqual(['today'])
    expect(done.earlierDone).toBe(1)

    // The Done filter is the whole history.
    const all = taskLanes(rows, { status: 'done', now: FIXTURE_DAY })[0]!
    expect(all.tasks.map(t => t.id).sort()).toEqual(['old', 'today'])
    expect(all.earlierDone).toBe(0)
  })

  it('reads completion time, never the last edit', () => {
    const rows = [
      // Completed last week, edited today: not today's.
      task({
        id: 'edited', status: 'done',
        completed_at: '2026-02-20T08:00:00Z', updated_at: '2026-03-01T08:00:00Z',
      }),
    ]
    expect(doneToday(rows[0]!, FIXTURE_DAY)).toBe(false)
    const done = taskLanes(rows, { status: 'all', now: FIXTURE_DAY }).find(l => l.status === 'done')!
    expect(done.tasks).toEqual([])
    expect(done.earlierDone).toBe(1)
  })

  it('counts an undated done task behind Show all, never as today, and lists it under the Done filter', () => {
    const untimed = task({ id: 'untimed', status: 'done', completed_at: null })
    expect(doneToday(untimed, FIXTURE_DAY)).toBe(false)
    const done = taskLanes([untimed], { status: 'all', now: FIXTURE_DAY }).find(l => l.status === 'done')!
    expect(done.tasks).toEqual([])
    expect(done.earlierDone).toBe(1)
    const filtered = taskLanes([untimed], { status: 'done', now: FIXTURE_DAY })[0]!
    expect(filtered.tasks.map(t => t.id)).toEqual(['untimed'])
    expect(filtered.earlierDone).toBe(0)
  })
})
