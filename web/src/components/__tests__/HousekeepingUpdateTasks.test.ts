// @vitest-environment jsdom
//
// Home → "After this update": one card per lifecycle, and the copy each state is
// allowed to use.
//
// The claims pinned here are the ones a card silently breaks:
//
//  - each lifecycle offers the *right* action, and only that one. `waiting_review`
//    must name the review queue (it has per-row accept/dismiss and a destination
//    picker) rather than sending the operator to re-decide proposals in prose.
//  - `unknown` is never drawn as "done" and never offers a Start button. Nobody
//    can say the work applies, and offering a button invites running instructions
//    for a condition this install has not established exists.
//  - a task the detector has ruled out is not a card at all. Every install ships
//    the whole catalog, so drawing one puts "nothing to do" on the Home of
//    everyone who never needed the task, and an install with no work would no
//    longer have an empty Home.
//  - a zero-task Home is empty — no group, no heading, no box. That includes
//    after the last card is hidden: the group may outlive its cards just long
//    enough to say what happened, and not a moment longer.
//  - a refusal stays on the card with its reason. A cleared card reads as
//    "handled", and a failed start is not handled.
//  - Hiding a task while a chat is open says in the confirmation that the chat is
//    untouched and not marked done, because both are surprising and neither is
//    obvious from a button labelled "Hide it".
//  - focus survives the card that the pressed button lived on disappearing.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import HousekeepingStrip from '../HousekeepingStrip.vue'
import { useHousekeepingStore } from '../../stores/housekeeping'
import { useProjectStore } from '../../stores/projects'
import type { UpdateTaskRow } from '../../lib/types'

// The router is imported lazily inside the click handlers, so it is stubbed as
// a module rather than injected: the component's own import is what the test
// needs to intercept, and a `vi.mock` here is the only thing that reaches it.
const routerPush = vi.hoisted(() => vi.fn(async () => {}))
vi.mock('../../router', () => ({ router: { push: routerPush } }))

// `api` is mocked so the tests that press a real button exercise the *real*
// store transition — the one that populates `taskErrors` and `pending…` — instead
// of a stubbed method that would leave the very state under test unset.
const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())
vi.mock('../../lib/api', () => ({ api: { get: apiGet, post: apiPost } }))

function task(overrides: Partial<UpdateTaskRow> = {}): UpdateTaskRow {
  return {
    id: 'review-legacy-rows',
    revision: 1,
    scope: 'workspace',
    title: 'Review the rows your old notes left behind',
    why: 'Rows retired into a folder no surface reads any more.',
    since_version: '2.1.0',
    status: 'offered',
    applicability: 'applicable',
    applicability_checked_at: '2026-09-01T10:00:00+00:00',
    offered: true,
    suppressed: false,
    chat_id: '',
    chat_live: false,
    prompt_digest: '',
    attempted_fingerprint: '',
    updated_at: '',
    ...overrides,
  }
}

beforeEach(() => {
  setActivePinia(createPinia())
  routerPush.mockClear()
  apiGet.mockReset()
  apiPost.mockReset()
  useProjectStore().activeWorkspace = 'personal'
  const housekeeping = useHousekeepingStore()
  // Avoid the interval / focus listeners firing during tests.
  vi.spyOn(housekeeping, 'init').mockImplementation(() => {})
})

afterEach(() => {
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

/** Mount with the task list preloaded, the way a fetched list arrives.
 *
 * `attachTo` is not incidental: the focus assertions below need the tree in the
 * document, because `focus()` on a detached node is a no-op and a test that
 * passes against one is not testing focus at all. */
async function mountGroup(rows: UpdateTaskRow[]) {
  const store = useHousekeepingStore()
  store.updateTasks = rows
  // The rows are stamped with the workspace they were computed for, and a
  // transition refuses without one — a nameless question is not sent.
  store.updateTasksWorkspace = 'personal'
  const host = document.createElement('div')
  document.body.appendChild(host)
  const wrapper = mount(HousekeepingStrip, { attachTo: host })
  await nextTick()
  return { wrapper, store }
}

function buttons(wrapper: Awaited<ReturnType<typeof mountGroup>>['wrapper']) {
  return wrapper.findAll('.update-task .housekeeping-actions button').map((b) => b.text())
}

function cards(wrapper: Awaited<ReturnType<typeof mountGroup>>['wrapper']) {
  return wrapper.findAll('.update-task')
}

describe('the group itself', () => {
  it('is absent entirely with zero tasks', async () => {
    // A fresh install ships no update tasks. An empty bordered box on Home is
    // worse than no box, and the group has no all-clear state to render, so its
    // absence never reads as "you are done".
    const { wrapper } = await mountGroup([])
    expect(wrapper.find('.update-tasks').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('After this update')
    wrapper.unmount()
  })

  it('is absent when every task is decided', async () => {
    // `dismissed` is hidden in this scope and `completed` is a verdict a
    // completion check reached. Neither is news on Home; both are in Settings.
    const { wrapper } = await mountGroup([
      task({ status: 'dismissed', suppressed: true }),
      task({ id: 'other', status: 'completed', suppressed: true }),
    ])
    expect(wrapper.find('.update-tasks').exists()).toBe(false)
    wrapper.unmount()
  })

  it('is absent when every task is one this install does not need', async () => {
    // The zero-work Home, and the reason it has to be zero: a catalog task the
    // detector ruled out is not a card, so a fresh install and an install that
    // needs none of the catalog look exactly the same — empty.
    const { wrapper } = await mountGroup([
      task({ applicability: 'not_applicable', offered: false, status: 'offered' }),
      task({ id: 'other', applicability: 'not_applicable', offered: false, status: 'offered' }),
    ])
    expect(wrapper.find('.update-tasks').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('After this update')
    wrapper.unmount()
  })

  it('keeps two revisions of one task as two cards', async () => {
    // A revision is a different record with its own decision, so collapsing them
    // would hide a task the operator still has to act on.
    const { wrapper } = await mountGroup([
      task({ revision: 1, status: 'dismissed', suppressed: true }),
      task({ revision: 2, status: 'offered' }),
    ])
    expect(cards(wrapper)).toHaveLength(1)
    expect(cards(wrapper)[0].text()).toContain('Review the rows')
    wrapper.unmount()
  })

  it('shows update cards without the redundant workspace lede', async () => {
    // The heading and cards already identify the work and its scope; a lede
    // restating the active workspace was the sentence this issue removed, and
    // re-adding one under the heading would put it back on Home.
    const { wrapper } = await mountGroup([task()])
    expect(wrapper.find('#update-tasks-heading').text()).toBe('After this update')
    expect(cards(wrapper)).toHaveLength(1)
    expect(cards(wrapper)[0].text()).toContain('Rows retired into a folder no surface reads any more.')
    expect(buttons(wrapper)).toContain('Start in chat')
    expect(wrapper.find('.update-tasks-lede').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('Work this version of Ciaobot left behind for')
    wrapper.unmount()
  })

  it('leaves ordinary housekeeping tiles and a blocking one alone', async () => {
    // The blocking tile is a precondition the install cannot get past on its
    // own: it has to stay prominent and non-dismissible, and adding the group
    // must not have given it a "Hide it" button.
    const store = useHousekeepingStore()
    store.actions = [{
      id: 'gate',
      kind: 'gate',
      severity: 1,
      title: 'Workspaces still share one vault',
      detail: 'Separate them.',
      glyph: '▲',
      workspace: 'personal',
      view_label: '',
      view_route: '',
      run_label: 'Separate them now',
      chat_label: '',
      chat_prompt: '',
      blocking: true,
    }]
    store.updateTasks = [task()]
    store.updateTasksWorkspace = 'personal'
    const wrapper = mount(HousekeepingStrip)
    await nextTick()

    const blocking = wrapper.find('.housekeeping-tile--blocking')
    expect(blocking.exists()).toBe(true)
    expect(blocking.text()).toContain('Separate them now')
    expect(blocking.text()).not.toContain('Hide it')
    // And the ordinary strip's own controls are unchanged.
    expect(blocking.findAll('.housekeeping-actions button').map((b) => b.text())).toEqual(['Separate them now'])
    expect(cards(wrapper as never)).toHaveLength(1)
    wrapper.unmount()
  })

  it('puts the group after the tiles, so a blocker is seen first', async () => {
    // An optional "start this task" card sorting above an unmissable
    // precondition would be the wrong reading order.
    const store = useHousekeepingStore()
    store.actions = [{
      id: 'gate', kind: 'gate', severity: 1, title: 'Blocker', detail: 'Fix.',
      glyph: '▲', workspace: 'personal', view_label: '', view_route: '',
      run_label: 'Fix now', chat_label: '', chat_prompt: '', blocking: true,
    }]
    store.updateTasks = [task()]
    store.updateTasksWorkspace = 'personal'
    const wrapper = mount(HousekeepingStrip)
    await nextTick()

    const html = wrapper.html()
    expect(html.indexOf('housekeeping-tile--blocking')).toBeLessThan(html.indexOf('update-tasks'))
    wrapper.unmount()
  })
})

describe('one card per lifecycle', () => {
  it('offers Start and Hide for a task that applies', async () => {
    const { wrapper } = await mountGroup([task()])
    expect(cards(wrapper)[0].text()).toContain('Review the rows your old notes left behind')
    expect(cards(wrapper)[0].text()).toContain('Rows retired into a folder no surface reads any more.')
    expect(cards(wrapper)[0].text()).toContain('Since Ciaobot 2.1.0')
    expect(buttons(wrapper)).toContain('Start in chat')
    expect(buttons(wrapper)).toContain('Hide it')
    expect(buttons(wrapper)).not.toContain('Check again')
    wrapper.unmount()
  })

  it('offers Resume and the chat for one already under way', async () => {
    const { wrapper } = await mountGroup([task({ status: 'in_progress', chat_id: 'chat-9', chat_live: true })])
    expect(buttons(wrapper)).toContain('Resume chat')
    expect(buttons(wrapper)).toContain('Open its chat')
    expect(buttons(wrapper)).not.toContain('Start in chat')
    expect(cards(wrapper)[0].text()).toContain('Started')
    wrapper.unmount()
  })

  it('stops claiming an archived chat is open, and does not offer to open it', async () => {
    // The record still names the chat its attempt opened, but archiving it means
    // there is no chat to open and a start mints a fresh one. "Its chat is open"
    // and "Open its chat" would both be about something that is not there.
    const { wrapper } = await mountGroup([
      task({ status: 'in_progress', chat_id: 'chat-9', chat_live: false }),
    ])
    expect(cards(wrapper)[0].text()).not.toContain('Its chat is open')
    expect(cards(wrapper)[0].text()).toContain('archived or deleted')
    expect(cards(wrapper)[0].text()).toContain('Starting opens a new one')
    expect(buttons(wrapper)).not.toContain('Open its chat')
    // The lead action is the same start that will mint a chat, so it says so.
    expect(buttons(wrapper)).not.toContain('Resume chat')
    expect(buttons(wrapper)).toContain('Start a new chat')
    wrapper.unmount()
  })

  it('sends waiting_review to the proposals surface, and offers Resume too', async () => {
    // The queue has per-row accept/dismiss and a destination picker. Telling
    // somebody to decide proposals in a chat, while the buttons for them sit one
    // route away, is the failure this button exists to avoid.
    const { wrapper, store } = await mountGroup([
      task({ status: 'waiting_review', chat_id: 'chat-9', chat_live: true }),
    ])
    expect(buttons(wrapper)).toContain('Review proposals')
    expect(buttons(wrapper)).toContain('Resume chat')
    expect(cards(wrapper)[0].text()).toContain('waiting for you to decide')

    await wrapper.findAll('button').find((b) => b.text() === 'Review proposals')!.trigger('click')
    expect(store.updateTasks).toHaveLength(1)
    wrapper.unmount()
  })

  it('shows the failure and a retry for an attempt that did not get going', async () => {
    const { wrapper } = await mountGroup([
      task({ status: 'failed', chat_id: 'chat-9', chat_live: true, updated_at: '2026-09-05T18:30:00+00:00' }),
    ])
    expect(buttons(wrapper)).toContain('Try again')
    expect(cards(wrapper)[0].text()).toContain('did not get going')
    expect(cards(wrapper)[0].text()).toContain('reuses the same chat')
    expect(buttons(wrapper)).toContain('Open its chat')
    wrapper.unmount()
  })

  it('offers only a re-check for an unknown applicability, and never "done"', async () => {
    // A detector that has not run has not established that there is nothing to
    // do. A Start button here would invite an operator to run instructions for
    // a condition this install cannot claim exists, and any "done"-flavoured
    // wording would be a lie about the silence.
    const { wrapper } = await mountGroup([
      task({ applicability: 'unknown', offered: false, status: 'offered' }),
    ])
    expect(buttons(wrapper)).toEqual(['Check again', 'Hide it'])
    expect(cards(wrapper)[0].text()).toContain('Checking whether this applies here')
    expect(cards(wrapper)[0].text()).not.toContain('New since this update')
    wrapper.unmount()
  })

  it('draws no card at all for an offer that does not apply here', async () => {
    // Every install ships the whole catalog, so a task the detector has ruled
    // out is not news. Drawn as a card it says "nothing to do" on the Home of
    // every install that never needed it, and never goes away — which is also
    // how an install with no work at all ends up with a non-empty Home.
    const { wrapper } = await mountGroup([
      task({ applicability: 'not_applicable', offered: false, status: 'offered' }),
    ])
    expect(cards(wrapper)).toHaveLength(0)
    expect(wrapper.find('.update-tasks').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('no longer applies')
    wrapper.unmount()
  })

  it('keeps Resume on a live attempt whose work the detector now says is done', async () => {
    // This is the normal shape once a chat has done the job: the detector sees
    // the condition is gone before the completion check has confirmed it. The
    // chat is still open and the operator is still in it, so the button that
    // gets them back has to be here — a "Check again" in its place would strand
    // a running task with no way back into it.
    const { wrapper } = await mountGroup([
      task({
        status: 'in_progress',
        applicability: 'not_applicable',
        offered: false,
        chat_id: 'chat-9', chat_live: true,
      }),
    ])
    expect(buttons(wrapper)).toContain('Resume chat')
    expect(buttons(wrapper)).toContain('Open its chat')
    expect(buttons(wrapper)).not.toContain('Check again')
    wrapper.unmount()
  })

  it('offers a retry on a failed attempt nobody can say applies', async () => {
    // `unknown` means nobody could answer, which is a reason not to *offer* the
    // work — not a reason to deny the retry of an attempt that already exists.
    // The server's start is idempotent per (task, revision), so a retry here
    // reuses that attempt's chat rather than starting a second one.
    const { wrapper } = await mountGroup([
      task({ status: 'failed', applicability: 'unknown', offered: false, chat_id: 'chat-9', chat_live: true }),
    ])
    expect(buttons(wrapper)).toContain('Try again')
    expect(buttons(wrapper)).not.toContain('Check again')
    wrapper.unmount()
  })

  it('marks the pressed row busy rather than leaving a dead button', async () => {
    // A card with a dead button and no feedback is the other half of "silently
    // cleared": the operator cannot tell a press from a no-op. The pending flag
    // is the store's, so the real transition is what is observed here — a stubbed
    // one would never set it and the test would pass for the wrong reason.
    const { wrapper, store } = await mountGroup([task()])
    // Typed rather than inferred: the assignment is inside a callback, so
    // control-flow analysis would narrow the `let` to `never`.
    const pending: { release: (() => void) | null } = { release: null }
    vi.spyOn(store, 'startUpdateTask').mockImplementation(() => new Promise((resolve) => {
      pending.release = () => resolve({ ok: false, chatId: '', resumed: false, error: 'nope' })
      // Mirror the store's own pending bookkeeping, which is what the button
      // reads; asserted directly in housekeeping.updateTasks.test.ts.
      store.pendingUpdateTaskIds = new Set(['review-legacy-rows@1'])
    }))

    const start = wrapper.findAll('button').find((b) => b.text() === 'Start in chat')!
    await start.trigger('click')
    await nextTick()

    expect(store.pendingUpdateTaskIds.has('review-legacy-rows@1')).toBe(true)
    expect(wrapper.findAll('button').find((b) => b.text() === 'Working…')!.attributes('disabled'))
      .toBeDefined()
    wrapper.unmount()
    pending.release?.()
  })
})

describe('a transition that did not happen', () => {
  it('keeps the card and shows the reason', async () => {
    // The refusal is stated on the card rather than swallowed: a card that
    // cleared itself would read as handled, and a failed start is not handled.
    // Driven through the real store transition, because `taskErrors` is what
    // this asserts and only the store writes it.
    const { wrapper } = await mountGroup([task()])
    apiPost.mockRejectedValueOnce(
      Object.assign(
        new Error('already completed at this revision'),
        { name: 'ApiError', status: 409, payload: { error: 'update task review-legacy-rows@1 is already completed at this revision' } },
      ),
    )

    await wrapper.findAll('button').find((b) => b.text() === 'Start in chat')!.trigger('click')
    await flushPromises()
    await nextTick()

    expect(cards(wrapper)).toHaveLength(1)
    const alert = wrapper.find('[role="alert"]')
    expect(alert.exists()).toBe(true)
    expect(alert.text()).toContain('already marked done')
    wrapper.unmount()
  })

  it('opens the chat a failed start left behind, and says why', async () => {
    // The 500 that carries a chat_id is the one case where the chat must be
    // opened rather than discarded: the next start retries into that same chat,
    // so a thrown-away id loses the task's only attempt.
    const { wrapper } = await mountGroup([task()])
    apiPost.mockRejectedValueOnce(
      Object.assign(
        new Error('the turn could not be dispatched'),
        { name: 'ApiError', status: 500, payload: { error: 'the turn could not be dispatched', chat_id: 'chat-9', chat_live: true } },
      ),
    )

    await wrapper.findAll('button').find((b) => b.text() === 'Start in chat')!.trigger('click')
    await flushPromises()

    expect(cards(wrapper)).toHaveLength(1)
    expect(routerPush).toHaveBeenCalledWith('/chat/chat-9')
    wrapper.unmount()
  })

  it('announces a dismissal in a live region', async () => {
    const { wrapper, store } = await mountGroup([task()])
    vi.spyOn(store, 'dismissUpdateTask').mockResolvedValue({
      ok: true, chatId: '', resumed: false, error: '',
    })
    vi.spyOn(await import('../../lib/confirm'), 'askConfirm').mockResolvedValue(true)

    await wrapper.findAll('button').find((b) => b.text() === 'Hide it')!.trigger('click')
    await flushPromises()
    await nextTick()

    expect(store.dismissUpdateTask).toHaveBeenCalled()
    expect(wrapper.find('.update-tasks-status').attributes('role')).toBe('status')
    expect(wrapper.find('.update-tasks-status').text()).toContain('Hidden')
    wrapper.unmount()
  })

  it('leaves the card alone when the dismissal is declined', async () => {
    const { wrapper, store } = await mountGroup([task()])
    const dismiss = vi.spyOn(store, 'dismissUpdateTask')
    vi.spyOn(await import('../../lib/confirm'), 'askConfirm').mockResolvedValue(false)

    await wrapper.findAll('button').find((b) => b.text() === 'Hide it')!.trigger('click')
    await flushPromises()

    expect(dismiss).not.toHaveBeenCalled()
    expect(cards(wrapper)).toHaveLength(1)
    wrapper.unmount()
  })
})

describe('what the hide confirmation says', () => {
  it('tells a running task that its chat is untouched and not marked done', async () => {
    // Two things are true at once and neither is obvious from a small button
    // labelled "Hide it": the card disappears for this workspace, and nothing
    // about the open chat changes. It is neither cancelled nor completed.
    const { wrapper, store } = await mountGroup([task({ status: 'in_progress', chat_id: 'chat-9', chat_live: true })])
    const confirm = vi.spyOn(await import('../../lib/confirm'), 'askConfirm').mockResolvedValue(true)
    vi.spyOn(store, 'dismissUpdateTask').mockResolvedValue({ ok: true, chatId: '', resumed: false, error: '' })

    await wrapper.findAll('button').find((b) => b.text() === 'Hide it')!.trigger('click')
    await flushPromises()

    const message = confirm.mock.calls[0][0] as string
    expect(message).toContain('chat stays open')
    expect(message).toContain('not marked done')
    expect(message).toContain('reopen')
    wrapper.unmount()
  })

  it('tells a task with no chat that hiding is scoped to this revision', async () => {
    const { wrapper } = await mountGroup([task()])
    const confirm = vi.spyOn(await import('../../lib/confirm'), 'askConfirm').mockResolvedValue(false)

    await wrapper.findAll('button').find((b) => b.text() === 'Hide it')!.trigger('click')
    await flushPromises()

    const message = confirm.mock.calls[0][0] as string
    expect(message).toContain('this revision only')
    expect(message).toContain('reopen it from Settings')
    wrapper.unmount()
  })

  it('labels the press as hiding, not completing', async () => {
    const { wrapper } = await mountGroup([task()])
    const confirm = vi.spyOn(await import('../../lib/confirm'), 'askConfirm').mockResolvedValue(false)
    await wrapper.findAll('button').find((b) => b.text() === 'Hide it')!.trigger('click')
    await flushPromises()
    expect(confirm.mock.calls[0][1]).toMatchObject({ confirmLabel: 'Hide it' })
    wrapper.unmount()
  })
})

describe('keyboard focus', () => {
  it('lands on the group when the pressed card disappears', async () => {
    // Hiding a task removes the very button that was pressed, so the browser
    // drops focus to the body and the next Tab restarts from the top of the
    // page — which reads, to somebody who just acted, as the app deciding they
    // were finished. The group stays for its announcement even with no cards
    // left, so it can take the focus back.
    const { wrapper } = await mountGroup([task()])
    vi.spyOn(await import('../../lib/confirm'), 'askConfirm').mockResolvedValue(true)
    apiPost.mockResolvedValueOnce({
      ok: true,
      task_id: 'review-legacy-rows',
      // The server's answer: the row is gone from this scope's list.
      tasks: [task({ status: 'dismissed', suppressed: true })],
    })

    const hide = wrapper.findAll('button').find((b) => b.text() === 'Hide it')!
    hide.element.focus()
    expect(document.activeElement).toBe(hide.element)

    await hide.trigger('click')
    await flushPromises()
    await nextTick()

    expect(cards(wrapper)).toHaveLength(0)
    // The group outlives its last card so the outcome has somewhere to be said
    // and focus has somewhere to land.
    const group = wrapper.find('.update-tasks')
    expect(group.exists()).toBe(true)
    expect(group.text()).toContain('Hidden')
    expect(document.activeElement).toBe(group.element)
    wrapper.unmount()
  })

  it('keeps focus on the button when the card stays', async () => {
    const { wrapper, store } = await mountGroup([task()])
    vi.spyOn(await import('../../lib/confirm'), 'askConfirm').mockResolvedValue(true)
    vi.spyOn(store, 'dismissUpdateTask').mockResolvedValue({ ok: true, chatId: '', resumed: false, error: '' })

    const hide = wrapper.findAll('button').find((b) => b.text() === 'Hide it')!
    hide.element.focus()
    await hide.trigger('click')
    await flushPromises()
    await nextTick()

    expect(document.activeElement).toBe(hide.element)
    wrapper.unmount()
  })

  it('gives the group a focusable landing spot of its own', async () => {
    // Focus restoration needs a target, and a <section> is not focusable by
    // default. tabindex="-1" is exactly that: reachable by script, not by Tab.
    const { wrapper } = await mountGroup([task()])
    const group = wrapper.find('.update-tasks')
    expect(group.attributes('tabindex')).toBe('-1')
    // Named by its own heading rather than a second copy of the string, so the
    // region name and the visible heading cannot drift apart.
    const labelledBy = group.attributes('aria-labelledby')
    expect(labelledBy).toBeTruthy()
    expect(wrapper.find(`#${labelledBy}`).text()).toBe('After this update')
    wrapper.unmount()
  })
})

describe('mobile and touch', () => {
  it('uses the same button classes as the strip, so touch sizing is shared', async () => {
    // 44px touch targets come from the global button tokens; a bespoke class
    // here would quietly opt the group's buttons out of them.
    const { wrapper } = await mountGroup([task()])
    const start = wrapper.findAll('button').find((b) => b.text() === 'Start in chat')!
    expect(start.classes()).toContain('btn-small')
    expect(start.classes()).toContain('btn-primary')
    const hide = wrapper.findAll('button').find((b) => b.text() === 'Hide it')!
    expect(hide.classes()).toContain('btn-chip')
    wrapper.unmount()
  })

  it('puts the group in the same measure as the strip above it', async () => {
    const { wrapper } = await mountGroup([task()])
    expect(wrapper.find('.update-tasks').classes()).toContain('update-tasks')
    wrapper.unmount()
  })
})

describe('the group after its last card goes', () => {
  it('stays long enough to say what happened, then goes', async () => {
    // The button that was pressed is the thing that disappears, so the outcome
    // needs somewhere to be said and focus needs somewhere to land. But a
    // heading, a lede and a stale "Hidden …" left standing on Home outlive the
    // thing they describe — and they are exactly what stopped a zero-task Home
    // from being empty again.
    //
    // Fake timers come first: the group status sets its own deadline, and one
    // armed against the real clock is not one these can advance.
    vi.useFakeTimers()
    try {
      const { wrapper } = await mountGroup([task()])
      vi.spyOn(await import('../../lib/confirm'), 'askConfirm').mockResolvedValue(true)
      apiPost.mockResolvedValueOnce({
        ok: true,
        task_id: 'review-legacy-rows',
        tasks: [task({ status: 'dismissed', suppressed: true })],
      })

      await wrapper.findAll('button').find((b) => b.text() === 'Hide it')!.trigger('click')
      await vi.advanceTimersByTimeAsync(0)
      await nextTick()

      expect(cards(wrapper)).toHaveLength(0)
      expect(wrapper.find('.update-tasks').text()).toContain('Hidden')

      // Ten seconds later it is gone, and Home is empty again.
      await vi.advanceTimersByTimeAsync(11_000)
      await nextTick()
      expect(wrapper.find('.update-tasks').exists()).toBe(false)
      expect(wrapper.text()).not.toContain('Hidden')
      expect(wrapper.text()).not.toContain('After this update')
      wrapper.unmount()
    } finally {
      vi.useRealTimers()
    }
  })

  it('never fires a timer after the strip is gone', async () => {
    // The timer holds a reference to a component that no longer exists, and
    // writing to it after unmount is a leak on a page the operator leaves and
    // comes back to every few minutes.
    const { wrapper, store } = await mountGroup([task()])
    vi.spyOn(await import('../../lib/confirm'), 'askConfirm').mockResolvedValue(true)
    vi.spyOn(store, 'dismissUpdateTask').mockResolvedValue({
      ok: true, chatId: '', resumed: false, error: '',
    })
    await wrapper.findAll('button').find((b) => b.text() === 'Hide it')!.trigger('click')
    await flushPromises()
    expect(wrapper.find('.update-tasks').text()).toContain('Hidden')

    wrapper.unmount()
    vi.useFakeTimers()
    try {
      expect(vi.getTimerCount()).toBe(0)
      // And nothing throws when the deadline that was pending passes.
      await vi.advanceTimersByTimeAsync(11_000)
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('the workspace switch', () => {
  it('re-asks rather than showing the previous workspace its answers', async () => {
    // The rows belong to the workspace they were computed for. Holding the
    // previous one's rows would show this workspace another one's answer.
    const store = useHousekeepingStore()
    const refresh = vi.spyOn(store, 'refreshUpdateTasks').mockResolvedValue(undefined)
    vi.spyOn(store, 'init').mockImplementation(() => {})
    const projects = useProjectStore()
    projects.activeWorkspace = 'personal'
    mount(HousekeepingStrip)
    await nextTick()
    refresh.mockClear()

    projects.activeWorkspace = 'work'
    await nextTick()
    expect(refresh).toHaveBeenCalledWith('work')
  })

  it('takes the last announcement with it, so it cannot cross workspaces', async () => {
    // "Hidden <task>" describes a press made in the workspace being left. Left
    // on screen it becomes a sentence about the previous workspace sitting under
    // the new one's name, which is worse than no sentence at all.
    const { wrapper, store } = await mountGroup([task()])
    vi.spyOn(await import('../../lib/confirm'), 'askConfirm').mockResolvedValue(true)
    vi.spyOn(store, 'dismissUpdateTask').mockResolvedValue({
      ok: true, chatId: '', resumed: false, error: '',
    })
    apiGet.mockResolvedValue({ tasks: [] })

    await wrapper.findAll('button').find((b) => b.text() === 'Hide it')!.trigger('click')
    await flushPromises()
    await nextTick()
    expect(wrapper.find('.update-tasks').text()).toContain('Hidden')

    useProjectStore().activeWorkspace = 'work'
    await flushPromises()
    await nextTick()

    expect(wrapper.find('.update-tasks').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('Hidden')
    wrapper.unmount()
  })

  it('does not announce a press whose answer lands in another workspace', async () => {
    // The press is real and the decision is recorded, but the sentence is about
    // the workspace it was made in, and by the time the answer comes back the
    // heading on screen names a different one. Clearing the status on the way to
    // announcing it would also throw away whatever the new workspace has to say
    // for itself, so the whole announcement is skipped — the effect (the chat it
    // opened) still happens.
    const { wrapper, store } = await mountGroup([task()])
    vi.spyOn(await import('../../lib/confirm'), 'askConfirm').mockResolvedValue(true)
    // The dismiss is still in flight when the operator moves on.
    const pending: { release: (() => void) | null } = { release: null }
    vi.spyOn(store, 'dismissUpdateTask').mockImplementation(() => new Promise((resolve) => {
      pending.release = () => resolve({ ok: true, chatId: '', resumed: false, error: '' })
    }))
    apiGet.mockResolvedValue({ tasks: [] })

    await wrapper.findAll('button').find((b) => b.text() === 'Hide it')!.trigger('click')
    await flushPromises()
    useProjectStore().activeWorkspace = 'work'
    await flushPromises()
    await nextTick()

    pending.release?.()
    await flushPromises()
    await nextTick()

    expect(store.dismissUpdateTask).toHaveBeenCalled()
    expect(wrapper.text()).not.toContain('Hidden')
    wrapper.unmount()
  })
})
