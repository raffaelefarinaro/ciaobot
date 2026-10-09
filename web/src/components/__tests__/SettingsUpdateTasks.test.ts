// @vitest-environment jsdom
//
// Settings → Home → "Update task history", mounted on its own. No
// SettingsView, no router, no fetch of anything but this card: the panel owns
// its own list, and the only thing that must be true of it is that it is honest
// about which clock it is showing.
//
// The claims pinned here:
//
//  - the list is grouped by scope, and a workspace operator can tell whose
//    records they are reading.
//  - `Checked` (when a detector last produced this row's answer) and `Decided`
//    (when the record was written) are shown as two different things. Rendering
//    one under the other's name would claim a re-check that never happened, or
//    hide a decision that did.
//  - a `completed` row is labelled "Verified" against the record's own time, and
//    a `dismissed` one says it was hidden — never "done", and never the same
//    timestamp printed twice under two names.
//  - Reopen is the only write. A refused reopen leaves the row and says why
//    rather than dropping it for having failed to change, and a successful one
//    refreshes the list Home draws from as well — a reopen that only this panel
//    sees is a task that stays hidden on the Home screen. It refreshes the list
//    Home is actually showing, though, never one belonging to a workspace the
//    operator has since left.
//  - a workspace switch takes the previous workspace's *whole* answer with it —
//    rows, the loaded flag, the errors and the pending presses — so the new
//    workspace is never described before it has been asked about.
//  - a failed read keeps the previous rows and shows the error, because an empty
//    history would claim nothing ever happened.
//  - the copy does not promise a fresh detector run: the engine reuses a cached
//    answer inside its own freshness window, so a check time may not move.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import SettingsUpdateTasks from '../settings/SettingsUpdateTasks.vue'
import { useHousekeepingStore } from '../../stores/housekeeping'
import { useProjectStore } from '../../stores/projects'
import type { UpdateTaskRow, UpdateTasksResponse } from '../../lib/types'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())
vi.mock('../../lib/api', () => ({ api: { get: apiGet, post: apiPost } }))

const routerPush = vi.hoisted(() => vi.fn(async () => {}))
vi.mock('../../router', () => ({ router: { push: routerPush } }))

function task(overrides: Partial<UpdateTaskRow> = {}): UpdateTaskRow {
  return {
    id: 'review-legacy-rows',
    revision: 1,
    scope: 'workspace',
    title: 'Review the rows your old notes left behind',
    why: 'Rows retired into a folder no surface reads any more.',
    since_version: '2.1.0',
    status: 'dismissed',
    applicability: 'not_applicable',
    applicability_checked_at: '2026-09-01T10:00:00+00:00',
    offered: false,
    suppressed: true,
    chat_id: 'chat-9',
    chat_live: true,
    prompt_digest: '',
    attempted_fingerprint: 'abc123',
    updated_at: '2026-09-05T18:30:00+00:00',
    ...overrides,
  }
}

function listing(rows: UpdateTaskRow[]): UpdateTasksResponse {
  return { tasks: rows }
}

function refused(status: number, body: Record<string, unknown>): Error {
  return Object.assign(new Error(String(body.error ?? 'refused')), {
    name: 'ApiError',
    status,
    payload: body,
  })
}

function buttonLabels(wrapper: ReturnType<typeof mount>): string[] {
  return wrapper.findAll('button').map((b) => b.text())
}

function button(wrapper: ReturnType<typeof mount>, label: string) {
  const found = wrapper.findAll('button').find((b) => b.text() === label)
  if (!found) throw new Error(`no button labelled ${label}`)
  return found
}

beforeEach(() => {
  setActivePinia(createPinia())
  useProjectStore().activeWorkspace = 'personal'
  apiGet.mockReset()
  apiPost.mockReset()
  routerPush.mockClear()
})

afterEach(() => {
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

describe('reading the list', () => {
  it('asks for the active workspace on mount', async () => {
    // The panel fetches for itself rather than reading the housekeeping store:
    // Home may never have been opened in this session, and a history that could
    // only be filled by visiting another page is not a record.
    apiGet.mockResolvedValue(listing([]))
    mount(SettingsUpdateTasks)
    await flushPromises()
    expect(apiGet).toHaveBeenCalledWith('/api/update-tasks?workspace=personal')
  })

  it('says it is checking, then shows an honest empty state', async () => {
    apiGet.mockResolvedValue(listing([]))
    const wrapper = mount(SettingsUpdateTasks)
    expect(wrapper.text()).toContain('Checking the update task history')
    await flushPromises()
    expect(wrapper.text()).toContain('Nothing to show here yet')
    wrapper.unmount()
  })

  it('re-asks when the workspace changes', async () => {
    apiGet.mockResolvedValue(listing([]))
    const wrapper = mount(SettingsUpdateTasks)
    await flushPromises()
    apiGet.mockClear()

    useProjectStore().activeWorkspace = 'work'
    await flushPromises()
    expect(apiGet).toHaveBeenCalledWith('/api/update-tasks?workspace=work')
    wrapper.unmount()
  })

  it('claims nothing about the new workspace while it is still asking', async () => {
    // A switch has to take the previous workspace's whole answer with it, not
    // just its rows. `loaded` still true meant the new workspace was told
    // "Nothing to show here yet." before anyone had asked about it, and the old
    // workspace's error stayed on screen under the new heading — a false empty
    // history and a stale complaint, both about a workspace this panel is not
    // showing.
    apiGet.mockResolvedValueOnce(listing([task({ status: 'dismissed' })]))
    apiPost.mockRejectedValueOnce(
      refused(409, { error: 'update task review-legacy-rows@1 is already completed at this revision' }),
    )
    const wrapper = mount(SettingsUpdateTasks)
    await flushPromises()
    await button(wrapper, 'Reopen').trigger('click')
    await flushPromises()
    expect(wrapper.find('[role="alert"]').text()).toContain('already marked done')

    // The new workspace's fetch, still open.
    apiGet.mockReturnValueOnce(new Promise(() => {}))
    useProjectStore().activeWorkspace = 'work'
    await flushPromises()

    expect(wrapper.text()).toContain('Checking the update task history')
    expect(wrapper.text()).not.toContain('Nothing to show here yet')
    expect(wrapper.findAll('.set-row')).toHaveLength(0)
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('leaves no row stuck on Reopening… across a workspace switch', async () => {
    // A press on the old workspace's row is not a press on the new one's. A
    // pending flag that outlived the switch would leave a button on the new
    // workspace's list that never comes back.
    apiGet.mockResolvedValueOnce(listing([task({ status: 'dismissed' })]))
    const pending: { release: (() => void) | null } = { release: null }
    apiPost.mockImplementationOnce(() => new Promise((resolve) => {
      pending.release = () => resolve({ ok: true, task_id: 'review-legacy-rows' })
    }))
    const wrapper = mount(SettingsUpdateTasks)
    await flushPromises()

    await button(wrapper, 'Reopen').trigger('click')
    await flushPromises()
    expect(buttonLabels(wrapper)).toContain('Reopening…')

    useProjectStore().activeWorkspace = 'work'
    await flushPromises()
    pending.release?.()
    await flushPromises()

    apiGet.mockResolvedValue(listing([task({ status: 'dismissed' })]))
    await button(wrapper, 'Check again').trigger('click')
    await flushPromises()
    expect(buttonLabels(wrapper)).toContain('Reopen')
    expect(buttonLabels(wrapper)).not.toContain('Reopening…')
    wrapper.unmount()
  })
})

describe('grouping by scope', () => {
  it('separates install-wide records from the workspace ones, install first', async () => {
    // A task the whole engine owns is a smaller set and a bigger decision, and
    // burying it under one workspace's rows would be the wrong order.
    apiGet.mockResolvedValue(listing([
      task({ id: 'ws-task', scope: 'workspace', status: 'dismissed' }),
      task({ id: 'install-task', scope: 'install', status: 'dismissed' }),
    ]))
    const wrapper = mount(SettingsUpdateTasks)
    await flushPromises()

    const labels = wrapper.findAll('.set-group-label').map((h) => h.text())
    expect(labels).toEqual(['This install', 'This workspace (personal)'])
    wrapper.unmount()
  })

  it('names the workspace so an operator with two can tell whose records these are', async () => {
    apiGet.mockResolvedValue(listing([task({ scope: 'workspace', status: 'dismissed' })]))
    const wrapper = mount(SettingsUpdateTasks)
    await flushPromises()
    expect(wrapper.text()).toContain('This workspace (personal)')
    wrapper.unmount()
  })

  it('omits a group with no rows in it', async () => {
    apiGet.mockResolvedValue(listing([task({ scope: 'workspace', status: 'dismissed' })]))
    const wrapper = mount(SettingsUpdateTasks)
    await flushPromises()
    expect(wrapper.findAll('.set-group-label')).toHaveLength(1)
    wrapper.unmount()
  })
})

describe('which tasks belong in a history', () => {
  it('leaves out the ones Home is showing right now', async () => {
    // A task still on offer, started, or waiting for review is not history.
    // Listing it here too would put the same live card in two places with two
    // different sets of buttons.
    apiGet.mockResolvedValue(listing([
      task({ id: 'a', status: 'offered', applicability: 'applicable', offered: true, suppressed: false }),
      task({ id: 'b', status: 'in_progress', applicability: 'applicable', offered: true, suppressed: false }),
      task({ id: 'c', status: 'waiting_review', applicability: 'applicable', offered: true, suppressed: false }),
    ]))
    const wrapper = mount(SettingsUpdateTasks)
    await flushPromises()
    expect(wrapper.text()).toContain('Nothing to show here yet')
    wrapper.unmount()
  })

  it('includes the genuinely uncertain, which is in on purpose', async () => {
    // A task that answers `unknown` is neither done nor to-do, and the only
    // place that can say so without nagging somebody on Home every sixty
    // seconds is a list they came to look at.
    apiGet.mockResolvedValue(listing([
      task({ id: 'u', status: 'offered', applicability: 'unknown', offered: false, suppressed: false }),
    ]))
    const wrapper = mount(SettingsUpdateTasks)
    await flushPromises()
    expect(wrapper.text()).toContain('Review the rows your old notes left behind')
    expect(wrapper.text()).toContain('Not checked')
    wrapper.unmount()
  })

  it('lists a completed, a dismissed and a failed task together', async () => {
    apiGet.mockResolvedValue(listing([
      task({ id: 'c', status: 'completed', applicability: 'not_applicable', suppressed: true }),
      task({ id: 'd', status: 'dismissed' }),
      task({ id: 'f', status: 'failed', applicability: 'applicable', offered: true, suppressed: false }),
    ]))
    const wrapper = mount(SettingsUpdateTasks)
    await flushPromises()
    await button(wrapper, 'Show 1 done').trigger('click')
    expect(wrapper.findAll('.set-row')).toHaveLength(3)
    expect(wrapper.text()).toContain('Done')
    expect(wrapper.text()).toContain('Hidden')
    expect(wrapper.text()).toContain('Attempt failed')
    wrapper.unmount()
  })
})

describe('the version a row dates from', () => {
  it('says which release a task is since', async () => {
    apiGet.mockResolvedValue(listing([task({ id: 'v', since_version: '2.1.0' })]))
    const wrapper = mount(SettingsUpdateTasks)
    await flushPromises()
    expect(wrapper.text()).toContain('Since Ciaobot 2.1.0')
    wrapper.unmount()
  })

  it('omits the Since text when the release is unknown, never 0.0.0', async () => {
    apiGet.mockResolvedValue(listing([task({ id: 'u', since_version: '' })]))
    const wrapper = mount(SettingsUpdateTasks)
    await flushPromises()
    const row = wrapper.findAll('.set-row')[0]
    expect(row.text()).not.toContain('Since Ciaobot')
    expect(wrapper.text()).not.toContain('0.0.0')
    expect(row.text()).toContain('Hidden')
    wrapper.unmount()
  })
})

describe('finished rows', () => {
  it('folds verified-done rows away until asked, keeping the rest in view', async () => {
    // A done row asks nothing of anyone; leading with it buries the hidden,
    // failed and uncertain rows that might.
    apiGet.mockResolvedValue(listing([
      task({ id: 'c', title: 'Finished one', status: 'completed', applicability: 'not_applicable' }),
      task({ id: 'd', title: 'Hidden one', status: 'dismissed' }),
    ]))
    const wrapper = mount(SettingsUpdateTasks)
    await flushPromises()

    expect(wrapper.text()).not.toContain('Finished one')
    expect(wrapper.text()).toContain('Hidden one')
    const toggle = button(wrapper, 'Show 1 done')
    expect(toggle.attributes('aria-expanded')).toBe('false')

    await toggle.trigger('click')
    expect(wrapper.text()).toContain('Finished one')
    await button(wrapper, 'Hide done').trigger('click')
    expect(wrapper.text()).not.toContain('Finished one')
    wrapper.unmount()
  })

  it('keeps a completed row whose applicability is unknown in view', async () => {
    apiGet.mockResolvedValue(listing([
      task({ title: 'Uncertain one', status: 'completed', applicability: 'unknown' }),
    ]))
    const wrapper = mount(SettingsUpdateTasks)
    await flushPromises()
    expect(wrapper.text()).toContain('Uncertain one')
    expect(buttonLabels(wrapper).some((l) => /done/i.test(l))).toBe(false)
    wrapper.unmount()
  })

  it('does not call an all-done history empty', async () => {
    apiGet.mockResolvedValue(listing([task({ status: 'completed' })]))
    const wrapper = mount(SettingsUpdateTasks)
    await flushPromises()
    expect(wrapper.text()).not.toContain('Nothing to show here yet')
    expect(buttonLabels(wrapper)).toContain('Show 1 done')
    wrapper.unmount()
  })
})

describe('the two clocks', () => {
  it('shows when the answer was checked beside when the record was decided', async () => {
    // `Checked` is when a detector last produced this row's answer; `Decided` is
    // when the record was written. A dismissal is a decision, not a re-check, and
    // a history that merged the two would claim somebody had looked again.
    // Pinned on a failed attempt, the one lifecycle where both clocks are
    // genuinely different moments: nobody has re-checked, and the record was
    // written when the attempt gave up.
    const now = new Date('2026-09-30T12:00:00Z')
    vi.setSystemTime(now)
    apiGet.mockResolvedValue(listing([task({
      status: 'failed',
      applicability: 'applicable',
      offered: true,
      suppressed: false,
      applicability_checked_at: '2026-09-30T09:00:00Z',
      updated_at: '2026-09-30T11:30:00Z',
    })]))
    const wrapper = mount(SettingsUpdateTasks)
    await flushPromises()

    expect(wrapper.text()).toContain('Checked 3 hours ago')
    expect(wrapper.text()).toContain('Decided 30 minutes ago')
    wrapper.unmount()
    vi.useRealTimers()
  })

  it('labels a completed row Verified against the record time, not the check', async () => {
    const now = new Date('2026-09-30T12:00:00Z')
    vi.setSystemTime(now)
    apiGet.mockResolvedValue(listing([task({
      status: 'completed',
      applicability: 'not_applicable',
      suppressed: true,
      applicability_checked_at: '2026-09-30T09:00:00Z',
      updated_at: '2026-09-30T11:00:00Z',
    })]))
    const wrapper = mount(SettingsUpdateTasks)
    await flushPromises()
    await button(wrapper, 'Show 1 done').trigger('click')

    // A completion check is what verified it, and the record's own time is when
    // that verdict was written — so "Verified" never borrows the check's stamp.
    expect(wrapper.text()).toContain('Verified 1 hour ago')
    expect(wrapper.text()).toContain('Checked 3 hours ago')
    wrapper.unmount()
    vi.useRealTimers()
  })

  it('says a dismissed row was hidden, never that it is done', async () => {
    const now = new Date('2026-09-30T12:00:00Z')
    vi.setSystemTime(now)
    apiGet.mockResolvedValue(listing([task({
      updated_at: '2026-09-30T10:00:00Z',
    })]))
    const wrapper = mount(SettingsUpdateTasks)
    await flushPromises()
    expect(wrapper.text()).toContain('Hidden by you')
    expect(wrapper.text()).toContain('hidden 2 hours ago')
    expect(wrapper.text()).not.toContain('Done')
    wrapper.unmount()
    vi.useRealTimers()
  })

  it('does not print a hidden row’s one decision twice', async () => {
    // The dismissal *is* the record write, so "hidden X" and "Decided X" are the
    // same moment under two names. Twice is worse than either alone: it reads as
    // two separate events, and it puts a "Decided" clock on a row whose only
    // decision was to stop being shown.
    const now = new Date('2026-09-30T12:00:00Z')
    vi.setSystemTime(now)
    apiGet.mockResolvedValue(listing([task({
      applicability_checked_at: '2026-09-30T09:00:00Z',
      updated_at: '2026-09-30T10:00:00Z',
    })]))
    const wrapper = mount(SettingsUpdateTasks)
    await flushPromises()
    expect(wrapper.text()).toContain('hidden 2 hours ago')
    expect(wrapper.text()).toContain('Checked 3 hours ago')
    expect(wrapper.text()).not.toContain('Decided')
    wrapper.unmount()
    vi.useRealTimers()
  })

  it('says a task that was never checked has never been checked', async () => {
    apiGet.mockResolvedValue(listing([task({
      applicability: 'unknown',
      status: 'offered',
      offered: false,
      suppressed: false,
      applicability_checked_at: '',
      updated_at: '',
    })]))
    const wrapper = mount(SettingsUpdateTasks)
    await flushPromises()
    expect(wrapper.text()).toContain('Checked never')
    wrapper.unmount()
  })
})

describe('reopening', () => {
  it('posts a reopen for a hidden task and re-lists', async () => {
    apiGet.mockResolvedValueOnce(listing([task({ status: 'dismissed' })]))
    apiPost.mockResolvedValue({ ok: true, task_id: 'review-legacy-rows' })
    apiGet.mockResolvedValueOnce(listing([task({ status: 'offered', offered: true, suppressed: false, applicability: 'applicable' })]))
    const wrapper = mount(SettingsUpdateTasks)
    await flushPromises()

    await button(wrapper, 'Reopen').trigger('click')
    await flushPromises()

    expect(apiPost).toHaveBeenCalledWith(
      '/api/update-tasks/review-legacy-rows/reopen?workspace=personal',
    )
    wrapper.unmount()
  })

  it('brings the Home group up to date, so a reopened card is there again', async () => {
    // Home draws the same records from the housekeeping store, which keeps its
    // own copy of them. Re-listing only this panel leaves a reopened task hidden
    // on Home until the next 60s poll or a window focus — and moving between
    // pages in the app triggers neither, so a reopen that worked would look like
    // it had done nothing. The store's rows are set directly, the way a fetched
    // list arrives, so this asserts the refresh and not the initial state.
    const housekeeping = useHousekeepingStore()
    housekeeping.updateTasks = [task({ status: 'dismissed' })]
    housekeeping.updateTasksWorkspace = 'personal'

    apiGet.mockResolvedValueOnce(listing([task({ status: 'dismissed' })]))
    const wrapper = mount(SettingsUpdateTasks)
    await flushPromises()
    apiGet.mockClear()
    // Every re-list from here answers with the reopened row.
    apiGet.mockResolvedValue(
      listing([task({ status: 'offered', offered: true, suppressed: false, applicability: 'applicable' })]),
    )
    apiPost.mockResolvedValueOnce({ ok: true, task_id: 'review-legacy-rows' })

    await button(wrapper, 'Reopen').trigger('click')
    await flushPromises()

    expect(housekeeping.updateTasks.map((row) => row.status)).toEqual(['offered'])
    // And this panel agrees: an offer that applies is not history, so the row
    // leaves the history and goes back to being a Home card.
    expect(wrapper.findAll('.set-row')).toHaveLength(0)
    wrapper.unmount()
  })

  it('does not drag Home back to the workspace a reopen was made in', async () => {
    // A switch between the press and its answer changes nothing about the
    // decision, but it changes what "the list Home is showing" is. Asking the
    // store for the workspace the reader just left would replace the rows it
    // legitimately holds for the new one with another workspace's answers.
    const housekeeping = useHousekeepingStore()
    housekeeping.updateTasks = [task({ id: 'work-row', status: 'dismissed' })]
    housekeeping.updateTasksWorkspace = 'work'

    apiGet.mockResolvedValueOnce(listing([task({ status: 'dismissed' })]))
    const wrapper = mount(SettingsUpdateTasks)
    await flushPromises()
    apiGet.mockClear()
    apiGet.mockResolvedValue(
      listing([task({ status: 'offered', offered: true, suppressed: false, applicability: 'applicable' })]),
    )
    // A reopen that lands after the switch.
    const pending: { release: (() => void) | null } = { release: null }
    apiPost.mockImplementationOnce(() => new Promise((resolve) => {
      pending.release = () => resolve({ ok: true, task_id: 'review-legacy-rows' })
    }))
    await button(wrapper, 'Reopen').trigger('click')
    useProjectStore().activeWorkspace = 'work'
    await flushPromises()
    pending.release?.()
    await flushPromises()

    expect(apiGet).not.toHaveBeenCalledWith('/api/update-tasks?workspace=personal')
    expect(housekeeping.updateTasksWorkspace).toBe('work')
    expect(housekeeping.updateTasks.map((row) => row.id)).toEqual(['work-row'])
    wrapper.unmount()
  })

  it('keeps the row and says why when the reopen is refused', async () => {
    // A task that is not dismissed has an attempt in flight or a verdict already
    // reached, and overriding either is a decision nobody asked for. The route
    // refuses rather than doing it, and the row must not vanish for having failed
    // to change.
    apiGet.mockResolvedValue(listing([task({ status: 'dismissed' })]))
    apiPost.mockRejectedValueOnce(
      refused(409, { error: 'update task review-legacy-rows@1 is already completed at this revision' }),
    )
    const wrapper = mount(SettingsUpdateTasks)
    await flushPromises()

    await button(wrapper, 'Reopen').trigger('click')
    await flushPromises()

    expect(wrapper.findAll('.set-row')).toHaveLength(1)
    const alert = wrapper.find('[role="alert"]')
    expect(alert.exists()).toBe(true)
    expect(alert.text()).toContain('already marked done')
    wrapper.unmount()
  })

  it('offers Recheck, not Reopen, on a row that is not hidden', async () => {
    // Reopen only means anything for a dismissal. Putting it on a done or failed
    // row would invite a click the server can only refuse.
    apiGet.mockResolvedValue(listing([task({ status: 'failed', applicability: 'applicable', offered: true, suppressed: false })]))
    const wrapper = mount(SettingsUpdateTasks)
    await flushPromises()
    expect(buttonLabels(wrapper)).toContain('Recheck')
    expect(buttonLabels(wrapper)).not.toContain('Reopen')
    wrapper.unmount()
  })

  it('re-lists on Recheck rather than deciding anything locally', async () => {
    apiGet.mockResolvedValue(listing([task({ status: 'failed', applicability: 'applicable' })]))
    const wrapper = mount(SettingsUpdateTasks)
    await flushPromises()
    apiGet.mockClear()

    await button(wrapper, 'Recheck').trigger('click')
    await flushPromises()

    // The server owns how fresh an answer may be; the client cannot force a
    // detector to re-run, and pretending otherwise would date the check wrongly.
    expect(apiPost).not.toHaveBeenCalled()
    expect(apiGet).toHaveBeenCalledWith('/api/update-tasks?workspace=personal')
    wrapper.unmount()
  })

  it('says the re-check may not move the check time', async () => {
    // The server reuses a cached answer inside its own freshness window
    // (`update_tasks.APPLICABILITY_TTL_S`), so a "Recheck" whose time does not
    // move is the honest outcome. Copy that promises a fresh run would leave an
    // operator reading a clock that did not advance as a bug.
    apiGet.mockResolvedValue(listing([task({ status: 'failed', applicability: 'applicable' })]))
    const wrapper = mount(SettingsUpdateTasks)
    await flushPromises()
    expect(wrapper.text()).toContain('freshness window')
    expect(wrapper.text()).toContain('does not always move')
    wrapper.unmount()
  })

  it('offers no write at all beyond a reopen', async () => {
    // Read-mostly by design. A history with a delete, or an "undo everything",
    // would be a second and far less careful way to reach the same records the
    // Home card already writes through.
    apiGet.mockResolvedValue(listing([task({ status: 'completed', suppressed: true })]))
    const wrapper = mount(SettingsUpdateTasks)
    await flushPromises()
    await button(wrapper, 'Show 1 done').trigger('click')
    const labels = buttonLabels(wrapper)
    expect(labels.some((l) => /delete|remove|reset|clear/i.test(l))).toBe(false)
    wrapper.unmount()
  })
})

describe('outcome links', () => {
  it('links the chat a dismissed task was in', async () => {
    // The chat is the record of what was actually done, and it is the only place
    // the attempt exists.
    apiGet.mockResolvedValue(listing([task({ status: 'dismissed', chat_id: 'chat-9' })]))
    const wrapper = mount(SettingsUpdateTasks)
    await flushPromises()

    await button(wrapper, 'Open its chat').trigger('click')
    // The router is imported lazily inside the handler, so the click awaits the
    // import before it pushes.
    await flushPromises()
    expect(routerPush).toHaveBeenCalledWith('/chat/chat-9')
    wrapper.unmount()
  })

  it('offers no chat link for a task that never got one', async () => {
    // A dismissal of a failed attempt drops its empty chat, so a link here would
    // open a chat the engine knows holds nothing.
    apiGet.mockResolvedValue(listing([task({ status: 'failed', chat_id: '', chat_live: false })]))
    const wrapper = mount(SettingsUpdateTasks)
    await flushPromises()
    expect(buttonLabels(wrapper)).not.toContain('Open its chat')
    wrapper.unmount()
  })

  it('offers no chat link for a chat that was archived or deleted', async () => {
    // The record deliberately keeps the id after the chat is gone, so `chat_id`
    // alone would keep offering a control that opens nothing. Liveness decides.
    apiGet.mockResolvedValue(
      listing([task({ status: 'dismissed', chat_id: 'gone-chat', chat_live: false })]),
    )
    const wrapper = mount(SettingsUpdateTasks)
    await flushPromises()
    expect(buttonLabels(wrapper)).not.toContain('Open its chat')
    wrapper.unmount()
  })
})

describe('a read that failed', () => {
  it('shows the error and keeps whatever it had', async () => {
    // An empty history would claim nothing ever happened, which is a different
    // and much worse statement than "we could not check".
    apiGet.mockResolvedValueOnce(listing([task({ status: 'dismissed' })]))
    const wrapper = mount(SettingsUpdateTasks)
    await flushPromises()
    expect(wrapper.findAll('.set-row')).toHaveLength(1)

    apiGet.mockRejectedValueOnce(new Error('offline'))
    await button(wrapper, 'Check again').trigger('click')
    await flushPromises()

    expect(wrapper.find('[role="alert"]').text()).toContain('Could not read the update task history')
    expect(wrapper.findAll('.set-row')).toHaveLength(1)
    wrapper.unmount()
  })

  it('offers a retry in the header', async () => {
    apiGet.mockRejectedValueOnce(new Error('offline'))
    const wrapper = mount(SettingsUpdateTasks)
    await flushPromises()
    expect(buttonLabels(wrapper)).toContain('Check again')
    expect(buttonLabels(wrapper)).not.toContain('Reopen')
    wrapper.unmount()
  })
})

describe('reading order and touch targets', () => {
  it('uses the shared settings row and button classes', async () => {
    // 44px touch targets and the hairline rows come from the shared sheet; a
    // bespoke class here would quietly opt the list out of both.
    apiGet.mockResolvedValue(listing([task({ status: 'dismissed' })]))
    const wrapper = mount(SettingsUpdateTasks)
    await flushPromises()

    expect(wrapper.find('.set-list').exists()).toBe(true)
    expect(wrapper.find('.set-row').exists()).toBe(true)
    expect(wrapper.find('.set-row-title').exists()).toBe(true)
    const reopen = button(wrapper, 'Reopen')
    expect(reopen.classes()).toContain('btn-small')
    expect(reopen.classes()).toContain('btn-primary')
    wrapper.unmount()
  })

  it('gives the state a text word, never colour alone', async () => {
    // DESIGN.md: color is never the only state signal. The tag carries the word
    // "Hidden", so a reader who cannot see the colour still knows the state.
    apiGet.mockResolvedValue(listing([task({ status: 'dismissed' })]))
    const wrapper = mount(SettingsUpdateTasks)
    await flushPromises()
    expect(wrapper.find('.set-tag').text()).toBe('Hidden')
    wrapper.unmount()
  })
})
