// @vitest-environment jsdom
//
// The Webhook triggers section inside Automations, mounted on its own: the
// section owns its list, so the only thing that must be true of it is that it is
// honest about a credential it is allowed to show exactly once.
//
// The claims pinned here:
//
//  - the five load states stay five. A first load in flight is not an empty
//    answer, a failed first load is not an empty answer, and a failed refresh
//    keeps the rows the pane is drawing.
//  - every write presents the revision the row was read at. A 409 keeps the row
//    and shows the server's own sentence rather than dropping it for having
//    failed to change, and delete sends the same revision on the query, because
//    the route reads it from there and nowhere else.
//  - create and rotate each reveal a secret once, in the dialog, and nothing else
//    ever holds it: not the store, not the list payload, not the DOM after the
//    dialog closes.
//  - rotate says the previous secret is dead, because a sender still holding it
//    gets the receiver's single 401 and nothing here can tell it which of the two
//    credentials it has.
//  - the recipe carries the real trigger id and the machine contract (bearer,
//    Idempotency-Key, the 202 receipt), and never a secret.
//  - the receipt history says what arrived, which chat it became and which one
//    needs a person — with the same five load states as the list above it, so a
//    history nobody has read is never drawn as a trigger that received nothing.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { DOMWrapper, flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import WebhookTriggers from '../WebhookTriggers.vue'
import { useProjectStore } from '../../stores/projects'
import { useWebhookStore } from '../../stores/webhooks'
import type { WebhookReceipt, WebhookTrigger } from '../../lib/types'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())
const apiPatch = vi.hoisted(() => vi.fn())
const apiDel = vi.hoisted(() => vi.fn())
const writeClipboard = vi.hoisted(() => vi.fn(async (_text: string) => true))
const askConfirm = vi.hoisted(() => vi.fn(async () => true))
const routerPush = vi.hoisted(() => vi.fn(async () => {}))

vi.mock('../../lib/api', () => ({
  api: { get: apiGet, post: apiPost, patch: apiPatch, del: apiDel },
}))
// The app's clipboard helper is the one that falls back to `execCommand` on an
// insecure origin, and Ciaobot is routinely opened over plain http on the LAN, so
// the copy path under test is the shared one rather than `navigator.clipboard`.
vi.mock('../../lib/codeCopy', () => ({ writeClipboard }))
// The confirm dialog lives in App.vue, not in the section, so a delete has to be
// answerable from here without a second dialog mounted.
vi.mock('../../lib/confirm', () => ({ askConfirm }))
// A chat the event became is opened by pushing the app's own deep link, which is
// what resolves it to a workspace and a transcript.
vi.mock('../../router', () => ({ router: { push: routerPush } }))

const SECRET = 'whsec_9f2c1d4e7a8b'
const NEW_SECRET = 'whsec_5b3a0c6d2e1f'

function trigger(overrides: Partial<WebhookTrigger> = {}): WebhookTrigger {
  return {
    trigger_id: 'trg_issue_1034',
    name: 'New issue from GitHub',
    workspace: 'personal',
    project_id: null,
    instructions: 'Triage the issue and open a note.',
    enabled: true,
    input_policy: 'event_text',
    created_at: '2026-03-01T09:00:00+00:00',
    updated_at: '2026-03-01T09:00:00+00:00',
    revision: 3,
    ...overrides,
  }
}

function listing(rows: WebhookTrigger[]): { triggers: WebhookTrigger[] } {
  return { triggers: rows }
}

/** One recorded event, as `GET /api/webhooks/{id}/receipts` returns it. */
function receipt(overrides: Partial<WebhookReceipt> = {}): WebhookReceipt {
  return {
    receipt_id: 'wbrcpt_1a2b3c',
    trigger_id: 'trg_issue_1034',
    trigger_name: 'New issue from GitHub',
    status: 'launched',
    chat_id: 'chat-from-the-event',
    event_text: 'the nightly build failed on main',
    created_at: '2026-03-04T09:00:00+00:00',
    updated_at: '2026-03-04T09:00:05+00:00',
    detail: 'chat chat-from-the-event',
    ...overrides,
  }
}

/**
 * The list read and the history read in one mock.
 *
 * Both go through `api.get`, so the history tests answer by URL rather than by
 * call order — a queue of `mockResolvedValueOnce` would make every test that
 * opens a history depend on how many reads preceded it.
 */
function serving(rows: WebhookReceipt[], options: { limit?: number; triggers?: WebhookTrigger[] } = {}) {
  const triggers = options.triggers ?? [trigger()]
  apiGet.mockImplementation(async (url: string) =>
    url.includes('/receipts')
      ? { limit: options.limit ?? 50, receipts: rows }
      : listing(triggers),
  )
}

/** An `ApiError` as `lib/api.ts` builds it: a flat `{"error": "…"}` body. */
function refused(status: number, message: string): Error {
  return Object.assign(new Error(message), {
    name: 'ApiError',
    status,
    payload: { error: message },
  })
}

/**
 * Mounted with `attachTo` and **no** Teleport stub.
 *
 * The row's delete is behind a Reka `DropdownMenu`, and both dialogs are in a
 * Reka `DialogPortal` — the pane has `container-type: inline-size`, so an
 * in-place `position: fixed` overlay would be clipped to the Automations pane and
 * leave the sidebar live (see `ChatLayout.vue`). Both teleport to `document.body`,
 * so stubbing Teleport would leave them permanently empty. The helpers below read
 * through `document`, which is where a reader's click and focus land anyway.
 */
function mountSection() {
  return mount(WebhookTriggers, { attachTo: document.body })
}

/**
 * Every button in the document, the portalled dialogs' included.
 *
 * Through `document` rather than `wrapper` because the dialogs are out of this
 * component's subtree, and a helper that only looked where the component happens
 * to render would quietly stop finding their controls at all. Wrapped in a
 * `DOMWrapper` so callers keep the usual `trigger`/`classes` vocabulary.
 */
function buttons(): DOMWrapper<HTMLButtonElement>[] {
  return Array.from(document.querySelectorAll<HTMLButtonElement>('button'))
    .map(el => new DOMWrapper<HTMLButtonElement>(el))
}

function button(_wrapper: ReturnType<typeof mountSection>, label: string) {
  const found = buttons().find(b => b.text() === label)
  if (!found) throw new Error(`no button labelled ${label}`)
  return found
}

function hasButton(_wrapper: ReturnType<typeof mountSection>, label: string): boolean {
  return buttons().some(b => b.text() === label)
}

function row(wrapper: ReturnType<typeof mountSection>, name: string) {
  const found = wrapper.findAll('.wh-row').find(r => r.text().includes(name))
  if (!found) throw new Error(`no row for ${name}`)
  return found
}

/** The open receipt history panel, or a throw naming what is on screen. */
function historyPanel(wrapper: ReturnType<typeof mountSection>): DOMWrapper<HTMLElement> {
  const found = wrapper.find('.wh-history')
  if (!found.exists()) throw new Error(`no history panel; the section shows "${wrapper.text()}"`)
  return found as DOMWrapper<HTMLElement>
}

/** Open the row's history and wait for the read it starts. */
async function openHistory(wrapper: ReturnType<typeof mountSection>, name = 'New issue from GitHub') {
  await row(wrapper, name).findAll('button').filter(b => b.text() === 'History')[0].trigger('click')
  await flushPromises()
}

/**
 * Open a row's overflow menu and press its one item.
 *
 * The menu portals to `document.body`, so its items are read through `document`
 * rather than `wrapper.findAll` — which is also where a reader's click lands.
 * Deleting a trigger is destructive and stays behind a menu (DESIGN.md:
 * destructive actions behind a menu), so the test opens the menu the way the
 * pointer does rather than the section dropping it for testability.
 */
async function pressMenuItem(wrapper: ReturnType<typeof mountSection>, rowName: string, label: string) {
  await row(wrapper, rowName).find('.wh-overflow').trigger('click')
  await flushPromises()
  const items = Array.from(document.querySelectorAll<HTMLButtonElement>('.wh-menu-item'))
  const item = items.find(b => b.textContent?.trim() === label)
  if (!item) {
    throw new Error(`no menu item labelled ${label}; menu shows ${items.map(i => i.textContent?.trim()).join(', ')}`)
  }
  item.click()
  await flushPromises()
}

/**
 * The open dialog, or `null`.
 *
 * Through `document` because the dialogs are portalled to `body` — see
 * {@link mountSection}. Reka unmounts a closed dialog's content (`Presence`), so
 * the first `.wh-card` in the document is the open one.
 */
/**
 * Every word on the page, dialogs included.
 *
 * `document` rather than `wrapper` for the same reason as {@link buttons}: the
 * portalled dialogs are the only place a secret is ever written, so a check
 * scoped to the component's own subtree would pass without ever looking at it.
 */
function pageText(): string {
  return document.body.textContent ?? ''
}

function openDialog(_wrapper: ReturnType<typeof mountSection>): DOMWrapper<HTMLElement> | null {
  const card = document.querySelector<HTMLElement>('.wh-card')
  return card ? new DOMWrapper<HTMLElement>(card) : null
}

beforeEach(() => {
  setActivePinia(createPinia())
  useProjectStore().activeWorkspace = 'personal'
  apiGet.mockReset()
  apiPost.mockReset()
  apiPatch.mockReset()
  apiDel.mockReset()
  writeClipboard.mockReset()
  writeClipboard.mockResolvedValue(true)
  askConfirm.mockReset()
  askConfirm.mockResolvedValue(true)
  routerPush.mockClear()
})

afterEach(() => {
  document.body.innerHTML = ''
  vi.restoreAllMocks()
})

describe('the list', () => {
  it('asks for the active workspace on mount', async () => {
    apiGet.mockResolvedValue(listing([]))
    const wrapper = mountSection()
    await flushPromises()
    expect(apiGet).toHaveBeenCalledWith('/api/webhooks?workspace=personal')
    wrapper.unmount()
  })

  it('re-asks when the workspace changes, and takes the old answer with it', async () => {
    apiGet.mockResolvedValue(listing([trigger()]))
    const wrapper = mountSection()
    await flushPromises()
    expect(wrapper.findAll('.wh-row')).toHaveLength(1)

    // The new workspace's read is left open: the point is what the pane draws
    // while nobody has answered for it yet.
    apiGet.mockReturnValueOnce(new Promise(() => {}))
    useProjectStore().activeWorkspace = 'work'
    await flushPromises()

    expect(apiGet).toHaveBeenCalledWith('/api/webhooks?workspace=work')
    // The rows on screen were the previous workspace's, and a write made from
    // one of them would present another workspace's trigger id to this one.
    expect(wrapper.findAll('.wh-row')).toHaveLength(0)
    wrapper.unmount()
  })

  it('names the project or General, and when the row last changed', async () => {
    const projectStore = useProjectStore()
    projectStore.projects = [{
      project_id: 'proj_board',
      name: 'Board',
      workspace: 'personal',
      context: '',
      created_at: '',
      order: 0,
      vault_folder: '',
    }]
    apiGet.mockResolvedValue(listing([
      trigger(),
      trigger({ trigger_id: 'trg_other', name: 'Board one', project_id: 'proj_board' }),
    ]))
    const wrapper = mountSection()
    await flushPromises()

    // `project_id: null` is the workspace's General, a real destination and not
    // "unset" — a row reading "unset" would tell the user the trigger has none.
    expect(row(wrapper, 'New issue from GitHub').text()).toContain('General')
    expect(row(wrapper, 'New issue from GitHub').text()).toContain('updated')
    expect(row(wrapper, 'Board one').text()).toContain('Board')
    expect(row(wrapper, 'Board one').text()).not.toContain('mode')
    wrapper.unmount()
  })

  it('says enabled or disabled in words, not by colour alone', async () => {
    apiGet.mockResolvedValue(listing([
      trigger(),
      trigger({ trigger_id: 'trg_off', name: 'Paused one', enabled: false }),
    ]))
    const wrapper = mountSection()
    await flushPromises()

    expect(row(wrapper, 'New issue from GitHub').text()).toContain('Enabled')
    expect(row(wrapper, 'Paused one').text()).toContain('Disabled')
    wrapper.unmount()
  })
})

describe('the five load states', () => {
  it('says it is checking rather than claiming an empty list', async () => {
    apiGet.mockReturnValueOnce(new Promise(() => {}))
    const wrapper = mountSection()
    await nextTick()

    expect(wrapper.text()).toContain('Loading webhook triggers')
    expect(wrapper.text()).not.toContain('No webhook triggers yet')
    // And nothing to act on while it is still asking: a create beside a pending
    // read would be a form whose row the read could then contradict.
    expect(hasButton(wrapper, 'New trigger')).toBe(false)
    wrapper.unmount()
  })

  it('shows the first-load failure and an empty list is never drawn from it', async () => {
    apiGet.mockRejectedValueOnce(refused(500, 'trigger store unreadable'))
    const wrapper = mountSection()
    await flushPromises()

    const alert = wrapper.find('[role="alert"]')
    expect(alert.text()).toContain('Could not load webhook triggers')
    expect(alert.text()).toContain('trigger store unreadable')
    expect(wrapper.text()).not.toContain('No webhook triggers yet')
    wrapper.unmount()
  })

  it('keeps the rows it has when a refresh fails, and offers a retry', async () => {
    apiGet.mockResolvedValueOnce(listing([trigger()]))
    const wrapper = mountSection()
    await flushPromises()
    expect(wrapper.findAll('.wh-row')).toHaveLength(1)

    // The pane is on screen with nothing wrong, so the failing read is a refresh
    // rather than a first load. `reload` is what a workspace switch and the Retry
    // button both go through, so driving it directly is the same path.
    apiGet.mockRejectedValueOnce(refused(500, 'trigger store unreadable'))
    await useWebhookStore().reload('personal')
    await flushPromises()

    // An empty list here would claim the workspace has no triggers, which is a
    // different and much worse statement than "we could not check".
    expect(wrapper.findAll('.wh-row')).toHaveLength(1)
    expect(wrapper.text()).toContain('Showing the last successful load')
    expect(wrapper.text()).toContain('trigger store unreadable')

    // And the retry is offered, so a transient failure is not a dead end.
    apiGet.mockResolvedValueOnce(listing([]))
    await button(wrapper, 'Retry').trigger('click')
    await flushPromises()
    expect(wrapper.text()).not.toContain('Showing the last successful load')
    expect(wrapper.findAll('.wh-row')).toHaveLength(0)
    wrapper.unmount()
  })

  it('says so when the workspace genuinely has none', async () => {
    apiGet.mockResolvedValue(listing([]))
    const wrapper = mountSection()
    await flushPromises()

    expect(wrapper.text()).toContain('No webhook triggers yet')
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('always offers the create form, so an empty list can be filled from here', async () => {
    apiGet.mockResolvedValue(listing([]))
    const wrapper = mountSection()
    await flushPromises()

    expect(hasButton(wrapper, 'New trigger')).toBe(true)
    wrapper.unmount()
  })

  it('keeps the create form out of a failed first load', async () => {
    // A list that has never been read has no workspace to create into from this
    // pane's point of view, and a form beside a refusal is a second thing to get
    // wrong. Retry is the whole answer there.
    apiGet.mockRejectedValueOnce(refused(500, 'trigger store unreadable'))
    const wrapper = mountSection()
    await flushPromises()

    expect(wrapper.find('.wh-form').exists()).toBe(false)
    expect(hasButton(wrapper, 'Retry')).toBe(true)
    expect(hasButton(wrapper, 'New trigger')).toBe(false)
    wrapper.unmount()
  })
})

describe('creating a trigger', () => {
  async function openForm(wrapper: ReturnType<typeof mountSection>) {
    await button(wrapper, 'New trigger').trigger('click')
    await nextTick()
  }

  it('sends the workspace, project and instructions the form holds, and no mode', async () => {
    apiGet.mockResolvedValue(listing([]))
    apiPost.mockResolvedValue({ trigger: trigger(), secret: SECRET })
    const wrapper = mountSection()
    await flushPromises()
    await openForm(wrapper)

    await wrapper.find('input[type="text"]').setValue('New issue from GitHub')
    await wrapper.find('textarea').setValue('Triage the issue and open a note.')
    // Each event runs in the new-chat default mode, so the form offers no choice.
    expect(wrapper.text()).not.toContain('Mode')
    await button(wrapper, 'Create trigger').trigger('click')
    await flushPromises()

    expect(apiPost).toHaveBeenCalledWith('/api/webhooks', {
      name: 'New issue from GitHub',
      workspace: 'personal',
      instructions: 'Triage the issue and open a note.',
    })
    wrapper.unmount()
  })

  it('names the project it was given, and omits the field for General', async () => {
    const projectStore = useProjectStore()
    projectStore.projects = [{
      project_id: 'proj_board',
      name: 'Board',
      workspace: 'personal',
      context: '',
      created_at: '',
      order: 0,
      vault_folder: '',
    }]
    apiGet.mockResolvedValue(listing([]))
    apiPost.mockResolvedValue({ trigger: trigger(), secret: SECRET })
    const wrapper = mountSection()
    await flushPromises()
    await openForm(wrapper)

    await wrapper.find('input[type="text"]').setValue('Filed from the board')
    await wrapper.findAll('select').at(0)!.setValue('proj_board')
    await button(wrapper, 'Create trigger').trigger('click')
    await flushPromises()

    expect(apiPost).toHaveBeenCalledWith('/api/webhooks', expect.objectContaining({
      project_id: 'proj_board',
    }))
    wrapper.unmount()
  })

  it('reveals the secret once, in the dialog, and adds the row', async () => {
    apiGet.mockResolvedValue(listing([]))
    apiPost.mockResolvedValue({ trigger: trigger(), secret: SECRET })
    const wrapper = mountSection()
    await flushPromises()
    await openForm(wrapper)
    await wrapper.find('input[type="text"]').setValue('New issue from GitHub')
    await button(wrapper, 'Create trigger').trigger('click')
    await flushPromises()

    const dialog = openDialog(wrapper)
    expect(dialog).not.toBeNull()
    expect(dialog!.text()).toContain('Shown once')
    expect(dialog!.text()).toContain(SECRET)
    expect(wrapper.findAll('.wh-row')).toHaveLength(1)
    wrapper.unmount()
  })

  it('keeps the secret when a refresh started while the create was in flight', async () => {
    apiGet.mockResolvedValue(listing([]))
    // The POST is left open and a read for the same workspace starts underneath
    // it. That overlap is reachable from the UI: the stale banner's Retry stays
    // clickable while the form is open, and so does a workspace switch.
    let answerPost!: (value: unknown) => void
    apiPost.mockReturnValueOnce(new Promise(resolve => { answerPost = resolve }))
    const wrapper = mountSection()
    await flushPromises()
    await openForm(wrapper)
    await wrapper.find('input[type="text"]').setValue('New issue from GitHub')
    await button(wrapper, 'Create trigger').trigger('click')
    await flushPromises()

    await useWebhookStore().reload('personal')
    await flushPromises()

    // The read decides the rows; the secret has no second copy and cannot be
    // re-read, so the answer still owes it to the user.
    answerPost({ trigger: trigger(), secret: SECRET })
    await flushPromises()

    expect(openDialog(wrapper)!.text()).toContain(SECRET)
    wrapper.unmount()
  })

  it('reports a create answered without a secret instead of showing an empty one', async () => {
    // An empty string is the server not answering with it, not an empty
    // credential: a dialog with nothing in it reads as the whole answer, and the
    // user has no way to tell the two apart.
    apiGet.mockResolvedValue(listing([]))
    apiPost.mockResolvedValue({ trigger: trigger(), secret: '' })
    const wrapper = mountSection()
    await flushPromises()
    await openForm(wrapper)
    await wrapper.find('input[type="text"]').setValue('New issue from GitHub')
    await button(wrapper, 'Create trigger').trigger('click')
    await flushPromises()

    expect(openDialog(wrapper)).toBeNull()
    expect(wrapper.find('[role="alert"]').text()).toContain('did not answer with a secret')
    wrapper.unmount()
  })

  it('says in the empty state that a new trigger starts disabled', async () => {
    // The store files it disabled, so a sender wired up straight away gets the
    // receiver's 401 and nothing here would say why.
    apiGet.mockResolvedValue(listing([]))
    const wrapper = mountSection()
    await flushPromises()

    expect(wrapper.text()).toContain('New triggers start disabled')
    expect(wrapper.text()).toContain('Enable')
    wrapper.unmount()
  })

  it('says the project cannot be changed later, and that events get new-chat permissions', async () => {
    apiGet.mockResolvedValue(listing([]))
    const wrapper = mountSection()
    await flushPromises()
    await openForm(wrapper)

    // The row has no Edit and the route does not read the project on a PATCH,
    // so the form is the only place this is ever said.
    expect(wrapper.text()).toContain('cannot be changed after the trigger is created')
    expect(wrapper.text()).toContain('same permissions as any new chat')
    wrapper.unmount()
  })

  it('keeps the form open and shows the sentence when the create is refused', async () => {
    apiGet.mockResolvedValue(listing([]))
    apiPost.mockRejectedValueOnce(refused(400, 'trigger name must be 1-120 characters'))
    const wrapper = mountSection()
    await flushPromises()
    await openForm(wrapper)
    await wrapper.find('input[type="text"]').setValue('x')
    await button(wrapper, 'Create trigger').trigger('click')
    await flushPromises()

    expect(wrapper.find('[role="alert"]').text()).toContain('trigger name must be 1-120')
    expect(wrapper.find('input[type="text"]').exists()).toBe(true)
    wrapper.unmount()
  })
})

describe('the one-time secret', () => {
  async function createAndReveal(wrapper: ReturnType<typeof mountSection>) {
    await button(wrapper, 'New trigger').trigger('click')
    await nextTick()
    await wrapper.find('input[type="text"]').setValue('New issue from GitHub')
    await button(wrapper, 'Create trigger').trigger('click')
    await flushPromises()
  }

  it('is gone from the page once the dialog closes', async () => {
    apiGet.mockResolvedValue(listing([]))
    apiPost.mockResolvedValue({ trigger: trigger(), secret: SECRET })
    const wrapper = mountSection()
    await flushPromises()
    await createAndReveal(wrapper)
    expect(pageText()).toContain(SECRET)

    await button(wrapper, 'Done').trigger('click')
    await flushPromises()

    // Not hidden, gone: the engine only ever sends it once, so there is nothing
    // left to re-show and a later render must not be able to produce it again.
    expect(pageText()).not.toContain(SECRET)
    expect(openDialog(wrapper)).toBeNull()
    wrapper.unmount()
  })

  it('is held by neither the store nor the list payload', async () => {
    apiGet.mockResolvedValue(listing([]))
    apiPost.mockResolvedValue({ trigger: trigger(), secret: SECRET })
    const wrapper = mountSection()
    await flushPromises()
    await createAndReveal(wrapper)

    const store = useWebhookStore()
    expect(JSON.stringify(store.triggers)).not.toContain(SECRET)
    expect(store.triggers.every(row => !('secret' in row))).toBe(true)
    // And the dialog, once closed, leaves nothing to copy: the button that
    // copied it is not offered again for a secret the store never kept.
    await button(wrapper, 'Done').trigger('click')
    await flushPromises()
    expect(store.triggers).toHaveLength(1)
    wrapper.unmount()
  })

  it('copies the secret on request', async () => {
    apiGet.mockResolvedValue(listing([]))
    apiPost.mockResolvedValue({ trigger: trigger(), secret: SECRET })
    const wrapper = mountSection()
    await flushPromises()
    await createAndReveal(wrapper)

    await button(wrapper, 'Copy secret').trigger('click')
    await flushPromises()

    expect(writeClipboard).toHaveBeenCalledWith(SECRET)
    expect(button(wrapper, 'Copied').exists()).toBe(true)
    wrapper.unmount()
  })

  it('says when the copy failed rather than claiming it worked', async () => {
    writeClipboard.mockResolvedValue(false)
    apiGet.mockResolvedValue(listing([]))
    apiPost.mockResolvedValue({ trigger: trigger(), secret: SECRET })
    const wrapper = mountSection()
    await flushPromises()
    await createAndReveal(wrapper)

    await button(wrapper, 'Copy secret').trigger('click')
    await flushPromises()

    expect(button(wrapper, 'Copy failed').exists()).toBe(true)
    wrapper.unmount()
  })

  it('says a new trigger starts disabled, so the 401 that follows is expected', async () => {
    // The store files every trigger disabled and the sender is the next thing the
    // user does, so the dialog is where the reason has to be.
    apiGet.mockResolvedValue(listing([]))
    apiPost.mockResolvedValue({ trigger: trigger(), secret: SECRET })
    const wrapper = mountSection()
    await flushPromises()
    await createAndReveal(wrapper)

    expect(openDialog(wrapper)!.text()).toContain('New triggers start disabled')
    expect(openDialog(wrapper)!.text()).toContain('Enable')
    wrapper.unmount()
  })

  it('focuses the copy button, and hands focus back to New trigger after a create', async () => {
    apiGet.mockResolvedValue(listing([]))
    apiPost.mockResolvedValue({ trigger: trigger(), secret: SECRET })
    const wrapper = mountSection()
    await flushPromises()
    await createAndReveal(wrapper)

    // The dialog exists to put this on the clipboard, and the submit that opened
    // it has just been unmounted with the rest of the form — so with nothing
    // hand-placed, focus would be on `BODY` and the trap would hold nothing.
    expect(document.activeElement?.textContent).toBe('Copy secret')

    await button(wrapper, 'Done').trigger('click')
    await flushPromises()

    // And back to the section's own control, not the top of the document.
    expect(document.activeElement?.textContent).toBe('New trigger')
    wrapper.unmount()
  })
})

describe('rotating', () => {
  it('asks first, because one click retires a credential that is working now', async () => {
    apiGet.mockResolvedValue(listing([trigger()]))
    const wrapper = mountSection()
    await flushPromises()

    askConfirm.mockResolvedValueOnce(false)
    await button(wrapper, 'Rotate').trigger('click')
    await flushPromises()

    // The warning used to be a `title`, which no touch reader ever sees and which
    // arrives after the secret is already dead.
    expect(askConfirm).toHaveBeenCalled()
    expect(apiPost).not.toHaveBeenCalled()
    expect(openDialog(wrapper)).toBeNull()
    wrapper.unmount()
  })

  it('presents the revision the row was read at and reveals a new secret', async () => {
    apiGet.mockResolvedValue(listing([trigger({ revision: 4 })]))
    apiPost.mockResolvedValue({ trigger: trigger({ revision: 5 }), secret: NEW_SECRET })
    const wrapper = mountSection()
    await flushPromises()

    await button(wrapper, 'Rotate').trigger('click')
    await flushPromises()

    expect(apiPost).toHaveBeenCalledWith('/api/webhooks/trg_issue_1034/rotate', {
      expected_revision: 4,
    })
    const dialog = openDialog(wrapper)
    expect(dialog!.text()).toContain(NEW_SECRET)
    wrapper.unmount()
  })

  it('says the previous secret stopped working, and the old one is nowhere', async () => {
    apiGet.mockResolvedValue(listing([trigger()]))
    apiPost.mockResolvedValue({ trigger: trigger({ revision: 4 }), secret: NEW_SECRET })
    const wrapper = mountSection()
    await flushPromises()

    await button(wrapper, 'Rotate').trigger('click')
    await flushPromises()

    // A sender holding the old value gets the receiver's single 401 and nothing
    // here can tell it which of the two credentials it has, so the rotation has
    // to say it out loud.
    const dialog = openDialog(wrapper)!
    expect(dialog.text()).toContain('The old secret is dead')
    expect(dialog.text()).toContain('Update every sender')
    expect(pageText()).not.toContain(SECRET)
    wrapper.unmount()
  })

  it('keeps the new secret when a refresh started while the rotation was in flight', async () => {
    apiGet.mockResolvedValue(listing([trigger({ revision: 4 })]))
    let answerPost!: (value: unknown) => void
    apiPost.mockReturnValueOnce(new Promise(resolve => { answerPost = resolve }))
    const wrapper = mountSection()
    await flushPromises()

    await button(wrapper, 'Rotate').trigger('click')
    await flushPromises()

    // Same overlap as a create under a refresh, and worse: by the time this
    // answers the old credential is already dead, so a lost new one leaves the
    // sender holding nothing that works and the pane saying nothing.
    await useWebhookStore().reload('personal')
    await flushPromises()

    answerPost({ trigger: trigger({ revision: 5 }), secret: NEW_SECRET })
    await flushPromises()

    const dialog = openDialog(wrapper)!
    expect(dialog.text()).toContain(NEW_SECRET)
    expect(dialog.text()).toContain('The old secret is dead')
    wrapper.unmount()
  })

  it('reports a rotation answered without the new secret, rather than silently', async () => {
    apiGet.mockResolvedValue(listing([trigger()]))
    apiPost.mockResolvedValue({ trigger: trigger({ revision: 4 }), secret: '' })
    const wrapper = mountSection()
    await flushPromises()

    await button(wrapper, 'Rotate').trigger('click')
    await flushPromises()

    expect(openDialog(wrapper)).toBeNull()
    expect(wrapper.find('[role="alert"]').text()).toContain('did not answer with the new one')
    wrapper.unmount()
  })

  it('keeps the row and shows the server sentence on a stale revision', async () => {
    apiGet.mockResolvedValue(listing([trigger({ revision: 4 })]))
    apiPost.mockRejectedValueOnce(refused(409, 'trigger trg_issue_1034 is at revision 7'))
    const wrapper = mountSection()
    await flushPromises()

    await button(wrapper, 'Rotate').trigger('click')
    await flushPromises()

    expect(wrapper.findAll('.wh-row')).toHaveLength(1)
    expect(wrapper.find('[role="alert"]').text()).toContain('is at revision 7')
    expect(openDialog(wrapper)).toBeNull()
    wrapper.unmount()
  })
})

describe('enabling and disabling', () => {
  it('is a revision-checked PATCH of the enabled flag alone', async () => {
    apiGet.mockResolvedValue(listing([trigger({ enabled: true, revision: 2 })]))
    apiPatch.mockResolvedValue({ trigger: trigger({ enabled: false, revision: 3 }) })
    const wrapper = mountSection()
    await flushPromises()

    await button(wrapper, 'Disable').trigger('click')
    await flushPromises()

    expect(apiPatch).toHaveBeenCalledWith('/api/webhooks/trg_issue_1034', {
      expected_revision: 2,
      enabled: false,
    })
    expect(wrapper.text()).toContain('Disabled')
    wrapper.unmount()
  })

  it('enables a disabled row the same way', async () => {
    apiGet.mockResolvedValue(listing([trigger({ enabled: false, revision: 6 })]))
    apiPatch.mockResolvedValue({ trigger: trigger({ enabled: true, revision: 7 }) })
    const wrapper = mountSection()
    await flushPromises()

    await button(wrapper, 'Enable').trigger('click')
    await flushPromises()

    expect(apiPatch).toHaveBeenCalledWith('/api/webhooks/trg_issue_1034', {
      expected_revision: 6,
      enabled: true,
    })
    expect(wrapper.text()).toContain('Enabled')
    wrapper.unmount()
  })

  it('keeps the row and shows the server sentence on a 409', async () => {
    apiGet.mockResolvedValue(listing([trigger({ enabled: true, revision: 2 })]))
    apiPatch.mockRejectedValueOnce(refused(409, 'trigger trg_issue_1034 is at revision 9'))
    const wrapper = mountSection()
    await flushPromises()

    await button(wrapper, 'Disable').trigger('click')
    await flushPromises()

    // A row must not vanish for having failed to change.
    expect(wrapper.findAll('.wh-row')).toHaveLength(1)
    expect(wrapper.find('[role="alert"]').text()).toContain('is at revision 9')
    wrapper.unmount()
  })

  it('sends no create-only field, so the project cannot be smuggled in', async () => {
    apiGet.mockResolvedValue(listing([trigger({ project_id: 'proj_board' })]))
    apiPatch.mockResolvedValue({ trigger: trigger({ enabled: false, revision: 4 }) })
    const wrapper = mountSection()
    await flushPromises()

    await button(wrapper, 'Disable').trigger('click')
    await flushPromises()

    const body = apiPatch.mock.calls[0][1] as Record<string, unknown>
    expect(Object.keys(body).sort()).toEqual(['enabled', 'expected_revision'])
    wrapper.unmount()
  })

  it('can be dismissed once read', async () => {
    apiGet.mockResolvedValue(listing([trigger()]))
    apiPatch.mockRejectedValueOnce(refused(409, 'stale'))
    const wrapper = mountSection()
    await flushPromises()
    await button(wrapper, 'Disable').trigger('click')
    await flushPromises()
    expect(wrapper.find('[role="alert"]').exists()).toBe(true)

    await button(wrapper, 'Dismiss').trigger('click')
    await nextTick()

    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    wrapper.unmount()
  })
})

describe('deleting', () => {
  it('asks first, and sends expected_revision on the query the route reads', async () => {
    apiGet.mockResolvedValue(listing([trigger({ revision: 8 })]))
    apiDel.mockResolvedValue(undefined)
    const wrapper = mountSection()
    await flushPromises()

    await pressMenuItem(wrapper, 'New issue from GitHub', 'Delete trigger…')

    expect(askConfirm).toHaveBeenCalled()
    expect(apiDel).toHaveBeenCalledWith('/api/webhooks/trg_issue_1034?expected_revision=8')
    expect(wrapper.findAll('.wh-row')).toHaveLength(0)
    wrapper.unmount()
  })

  it('does nothing at all when the confirmation is declined', async () => {
    askConfirm.mockResolvedValue(false)
    apiGet.mockResolvedValue(listing([trigger()]))
    const wrapper = mountSection()
    await flushPromises()

    await pressMenuItem(wrapper, 'New issue from GitHub', 'Delete trigger…')

    expect(apiDel).not.toHaveBeenCalled()
    expect(wrapper.findAll('.wh-row')).toHaveLength(1)
    wrapper.unmount()
  })

  it('keeps the row and shows the sentence when the delete is refused', async () => {
    apiGet.mockResolvedValue(listing([trigger({ revision: 2 })]))
    apiDel.mockRejectedValueOnce(refused(409, 'trigger trg_issue_1034 is at revision 5'))
    const wrapper = mountSection()
    await flushPromises()

    await pressMenuItem(wrapper, 'New issue from GitHub', 'Delete trigger…')

    expect(wrapper.findAll('.wh-row')).toHaveLength(1)
    expect(wrapper.find('[role="alert"]').text()).toContain('is at revision 5')
    wrapper.unmount()
  })
})

describe('the receiver recipe', () => {
  it('shows the real trigger id, the bearer, the idempotency key and the receipt', async () => {
    apiGet.mockResolvedValue(listing([trigger()]))
    const wrapper = mountSection()
    await flushPromises()

    await button(wrapper, 'Recipe').trigger('click')
    await flushPromises()

    const dialog = openDialog(wrapper)!
    expect(dialog.text()).toContain('POST /hooks/v1/trg_issue_1034')
    expect(dialog.text()).toContain('Authorization: Bearer')
    expect(dialog.text()).toContain('Idempotency-Key')
    expect(dialog.text()).toContain('"text"')
    expect(dialog.text()).toContain('receipt_id')
    expect(dialog.text()).toContain('202')
    wrapper.unmount()
  })

  it('never carries a secret, not even the one the dialog just showed', async () => {
    apiGet.mockResolvedValue(listing([trigger()]))
    apiPost.mockResolvedValue({ trigger: trigger(), secret: SECRET })
    const wrapper = mountSection()
    await flushPromises()
    await button(wrapper, 'New trigger').trigger('click')
    await nextTick()
    await wrapper.find('input[type="text"]').setValue('New issue from GitHub')
    await button(wrapper, 'Create trigger').trigger('click')
    await flushPromises()
    await button(wrapper, 'Done').trigger('click')
    await flushPromises()

    await button(wrapper, 'Recipe').trigger('click')
    await flushPromises()

    // The recipe outlives the copy button, and a credential in it would be a
    // second copy of a secret the engine only sends once.
    expect(openDialog(wrapper)!.text()).toContain('<secret>')
    expect(pageText()).not.toContain(SECRET)
    wrapper.unmount()
  })

  it('focuses Copy recipe, the control the dialog exists for', async () => {
    apiGet.mockResolvedValue(listing([trigger()]))
    const wrapper = mountSection()
    await flushPromises()

    await button(wrapper, 'Recipe').trigger('click')
    await flushPromises()

    // Reka's own first stop would be `Done`, which only closes the dialog.
    expect(document.activeElement?.textContent).toBe('Copy recipe')
    wrapper.unmount()
  })

  it('portals out of the pane, so the backdrop covers the window and not the pane', async () => {
    apiGet.mockResolvedValue(listing([trigger()]))
    const wrapper = mountSection()
    await flushPromises()
    await button(wrapper, 'Recipe').trigger('click')
    await flushPromises()

    // `.chat-main` declares `container-type: inline-size`, which makes it the
    // containing block for `position: fixed` descendants: an overlay drawn in
    // place would dim and clip to the Automations pane and leave the sidebar
    // live and undimmed behind a "modal".
    const backdrop = document.querySelector('.wh-backdrop')
    expect(backdrop).not.toBeNull()
    expect(wrapper.element.contains(backdrop)).toBe(false)
    wrapper.unmount()
  })

  it('copies the recipe text', async () => {
    apiGet.mockResolvedValue(listing([trigger()]))
    const wrapper = mountSection()
    await flushPromises()

    await button(wrapper, 'Recipe').trigger('click')
    await flushPromises()
    await button(wrapper, 'Copy recipe').trigger('click')
    await flushPromises()

    const copied = String(writeClipboard.mock.calls.at(-1)?.[0])
    expect(copied).toContain('POST /hooks/v1/trg_issue_1034')
    expect(copied).toContain('Idempotency-Key')
    expect(button(wrapper, 'Copied').exists()).toBe(true)
    wrapper.unmount()
  })

  it('toggles the row label so it can be closed again', async () => {
    apiGet.mockResolvedValue(listing([trigger()]))
    const wrapper = mountSection()
    await flushPromises()

    await button(wrapper, 'Recipe').trigger('click')
    await flushPromises()
    expect(button(wrapper, 'Hide recipe').exists()).toBe(true)
    expect(openDialog(wrapper)!.text()).toContain('trg_issue_1034')

    await button(wrapper, 'Hide recipe').trigger('click')
    await flushPromises()
    expect(openDialog(wrapper)).toBeNull()
    expect(button(wrapper, 'Recipe').exists()).toBe(true)
    wrapper.unmount()
  })
})

describe('the receipt history', () => {
  it('reads one trigger on demand, scoped to the workspace, and names its rows', async () => {
    serving([
      receipt(),
      receipt({
        receipt_id: 'wbrcpt_older',
        status: 'failed',
        chat_id: null,
        event_text: 'a rejected event',
        detail: 'the project does not exist',
      }),
    ])
    const wrapper = mountSection()
    await flushPromises()
    // Nothing is read until a history is opened: events arrive while a person is
    // looking at a trigger, so every row carrying its own read would be a request
    // per trigger on every pane visit.
    expect(apiGet).toHaveBeenCalledTimes(1)

    await openHistory(wrapper)

    expect(apiGet).toHaveBeenCalledWith('/api/webhooks/trg_issue_1034/receipts?workspace=personal')

    const panel = historyPanel(wrapper)
    expect(panel.text()).toContain('the nightly build failed on main')
    expect(panel.text()).toContain('a rejected event')
    // An outcome in words, newest first, never a colour alone.
    const rows = panel.findAll('.wh-receipt')
    expect(rows).toHaveLength(2)
    expect(rows[0].text()).toContain('Launched')
    expect(rows[1].text()).toContain('Failed')
    wrapper.unmount()
  })

  it('opens the chat a launched event became, and offers no button where none is', async () => {
    serving([
      receipt(),
      receipt({ receipt_id: 'wbrcpt_stuck', status: 'interrupted', chat_id: null, detail: 'review before retrying' }),
    ])
    const wrapper = mountSection()
    await flushPromises()
    await openHistory(wrapper)

    const panel = historyPanel(wrapper)
    expect(panel.findAll('.wh-receipt')[0].findAll('button')).toHaveLength(1)

    await panel.findAll('.wh-receipt')[0].find('button').trigger('click')
    await flushPromises()

    expect(routerPush).toHaveBeenCalledWith('/chat/chat-from-the-event')
    // An interrupted receipt names no chat: a button that cannot open anything is
    // worse than no button, and there is nothing to open.
    expect(panel.findAll('.wh-receipt')[1].find('button').exists()).toBe(false)
    wrapper.unmount()
  })

  it('shows an interrupted event, in words, rather than dropping it', async () => {
    // The one state nothing else reports and nobody replays: a process died
    // inside the launch window, so a person has to look at it.
    serving([
      receipt({
        receipt_id: 'wbrcpt_stuck',
        status: 'interrupted',
        chat_id: null,
        detail: 'the launch allocation was recorded but the outcome was not',
      }),
    ])
    const wrapper = mountSection()
    await flushPromises()
    await openHistory(wrapper)

    const panel = historyPanel(wrapper)
    expect(panel.text()).toContain('Interrupted')
    expect(panel.text()).toContain('the launch allocation was recorded but the outcome was not')
    expect(panel.findAll('.wh-receipt')).toHaveLength(1)
    wrapper.unmount()
  })

  it('says the read is capped once the rows fill it', async () => {
    // A history that hit its cap and stopped is otherwise indistinguishable from a
    // trigger that received exactly that many events ever.
    serving([receipt()], { limit: 1 })
    const wrapper = mountSection()
    await flushPromises()
    await openHistory(wrapper)

    expect(historyPanel(wrapper).text()).toContain('Showing up to 1 recent events; older retained events are not shown.')
    expect(historyPanel(wrapper).text()).not.toContain('Older ones are in the journal')
    wrapper.unmount()
  })

  it('says it is checking rather than claiming the trigger received nothing', async () => {
    apiGet.mockImplementation(async (url: string) => {
      if (url.includes('/receipts')) return new Promise(() => {})
      return listing([trigger()])
    })
    const wrapper = mountSection()
    await flushPromises()
    await openHistory(wrapper)

    const panel = historyPanel(wrapper)
    expect(panel.text()).toContain('Loading received events')
    expect(panel.text()).not.toContain('No events yet')
    wrapper.unmount()
  })

  it('shows a first read that failed, and never an empty answer from it', async () => {
    apiGet.mockImplementation(async (url: string) => {
      if (url.includes('/receipts')) throw refused(500, 'the receipt journal could not be read')
      return listing([trigger()])
    })
    const wrapper = mountSection()
    await flushPromises()
    await openHistory(wrapper)

    const alert = wrapper.find('.wh-history [role="alert"]')
    expect(alert.text()).toContain('Could not load received events')
    expect(alert.text()).toContain('the receipt journal could not be read')
    expect(historyPanel(wrapper).text()).not.toContain('No events yet')

    // Retry is the whole answer, and it reads again.
    serving([receipt()])
    await button(wrapper, 'Retry').trigger('click')
    await flushPromises()
    expect(historyPanel(wrapper).text()).toContain('the nightly build failed on main')
    wrapper.unmount()
  })

  it('keeps the rows it has when a refresh fails, and says they are stale', async () => {
    serving([receipt()])
    const wrapper = mountSection()
    await flushPromises()
    await openHistory(wrapper)
    expect(historyPanel(wrapper).findAll('.wh-receipt')).toHaveLength(1)

    await button(wrapper, 'Hide history').trigger('click')
    await flushPromises()
    apiGet.mockImplementation(async (url: string) => {
      if (url.includes('/receipts')) throw refused(500, 'the receipt journal could not be read')
      return listing([trigger()])
    })
    await openHistory(wrapper)

    // The rows on screen are older than the answer that failed, and the panel says
    // so: an empty history here would claim the trigger received nothing at all.
    const panel = historyPanel(wrapper)
    expect(panel.findAll('.wh-receipt')).toHaveLength(1)
    expect(panel.text()).toContain('Showing the last successful load')
    expect(panel.find('[role="alert"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('says so when a trigger genuinely has received nothing', async () => {
    serving([])
    const wrapper = mountSection()
    await flushPromises()
    await openHistory(wrapper)

    const panel = historyPanel(wrapper)
    expect(panel.text()).toContain('No events yet')
    // And the reason a person needs: a sender's accepted event appears here.
    expect(panel.text()).toContain('appears here the moment it')
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('drops a late answer from the workspace that was left behind', async () => {
    let answerReceipts!: (value: unknown) => void
    apiGet.mockImplementation(async (url: string) => {
      if (url.includes('/receipts')) {
        return new Promise(resolve => { answerReceipts = resolve })
      }
      return listing([trigger()])
    })
    const wrapper = mountSection()
    await flushPromises()
    await openHistory(wrapper)

    // A `1`–`9` shortcut moves the pane while the read is open. The trigger id
    // means nothing in the workspace that replaced it, so its answer is not that
    // workspace's history.
    useProjectStore().activeWorkspace = 'work'
    await flushPromises()
    expect(wrapper.find('.wh-history').exists()).toBe(false)

    answerReceipts({ limit: 50, receipts: [receipt()] })
    await flushPromises()

    expect(wrapper.find('.wh-history').exists()).toBe(false)
    expect(wrapper.findAll('.wh-receipt')).toHaveLength(0)
    expect(wrapper.text()).not.toContain('the nightly build failed on main')
    expect(useWebhookStore().receipts).toHaveLength(0)
    wrapper.unmount()
  })

  it('never draws one trigger’s rows under another', async () => {
    // One history at a time: the store holds a single one, keyed by trigger, so
    // opening a second cannot leave the first one's rows on screen under a row
    // they have nothing to do with.
    let answerSecond!: (value: unknown) => void
    apiGet.mockImplementation(async (url: string) => {
      if (url.includes('/trg_second/receipts')) return new Promise(resolve => { answerSecond = resolve })
      if (url.includes('/receipts')) return { limit: 50, receipts: [receipt()] }
      return listing([trigger(), trigger({ trigger_id: 'trg_second', name: 'Second trigger' })])
    })
    const wrapper = mountSection()
    await flushPromises()
    await openHistory(wrapper)
    expect(historyPanel(wrapper).text()).toContain('the nightly build failed on main')

    await openHistory(wrapper, 'Second trigger')

    // Its own read is asked for (events arrive while a person looks) and, until it
    // answers, the panel is a pending question — not the previous trigger's rows.
    expect(apiGet).toHaveBeenCalledWith('/api/webhooks/trg_second/receipts?workspace=personal')
    expect(historyPanel(wrapper).text()).toContain('Loading received events')
    expect(historyPanel(wrapper).text()).not.toContain('the nightly build failed on main')

    answerSecond({ limit: 50, receipts: [receipt({ receipt_id: 'wbrcpt_second', trigger_id: 'trg_second', event_text: 'the other trigger’s event' })] })
    await flushPromises()
    expect(historyPanel(wrapper).text()).toContain('the other trigger’s event')
    wrapper.unmount()
  })

  it('is a keyboard-reachable disclosure with 44px targets', async () => {
    serving([receipt()])
    const wrapper = mountSection()
    await flushPromises()

    // A native button with the expanded state named, and the panel it controls
    // named by id — so a screen reader is told what the press will do and which
    // element it owns.
    const toggle = row(wrapper, 'New issue from GitHub').findAll('button')
      .find(b => b.text() === 'History')!
    expect(toggle.attributes('aria-expanded')).toBe('false')
    // The shared button class is what carries the touch floor (App.vue gives
    // `.btn-small` `min-height: var(--touch)` on coarse pointers), so a bespoke
    // class here would quietly opt the history out of it.
    expect(toggle.classes()).toContain('btn-small')

    await toggle.trigger('click')
    await flushPromises()
    const panel = historyPanel(wrapper)
    expect(panel.attributes('id')).toBe('wh-history-trg_issue_1034')
    const opened = row(wrapper, 'New issue from GitHub').findAll('button')
      .find(b => b.text() === 'Hide history')!
    expect(opened.attributes('aria-controls')).toBe('wh-history-trg_issue_1034')
    expect(opened.attributes('aria-expanded')).toBe('true')
    // A native button is focusable, so a keyboard can carry from the disclosure
    // straight into the panel's own controls.
    opened.element.focus()
    expect(document.activeElement).toBe(opened.element)
    const openChatButton = panel.findAll('.wh-receipt')[0].find('button')
    expect(openChatButton.element.tagName).toBe('BUTTON')
    expect(openChatButton.classes()).toContain('btn-small')

    // And the same press closes it again, rather than leaving a disclosure the
    // reader cannot undo.
    await button(wrapper, 'Hide history').trigger('click')
    await flushPromises()
    expect(wrapper.find('.wh-history').exists()).toBe(false)
    wrapper.unmount()
  })
})

describe('reading order and touch targets', () => {
  it('uses the shared overview row and button classes', async () => {
    // 44px targets and the hairline row come from ./overviewSections.css; a
    // bespoke class here would quietly opt the section out of both.
    apiGet.mockResolvedValue(listing([trigger()]))
    const wrapper = mountSection()
    await flushPromises()

    expect(wrapper.find('.ov-section').exists()).toBe(true)
    expect(wrapper.find('.ov-head').exists()).toBe(true)
    const disable = button(wrapper, 'Disable')
    expect(disable.classes()).toContain('btn-small')
    const create = button(wrapper, 'New trigger')
    expect(create.classes()).toContain('btn-small')
    wrapper.unmount()
  })

  it('keeps Cancel reachable next to the create primary action', async () => {
    apiGet.mockResolvedValue(listing([]))
    const wrapper = mountSection()
    await flushPromises()
    await button(wrapper, 'New trigger').trigger('click')
    await nextTick()

    // A form whose only way out is the primary action is a form somebody gets
    // stuck in after a refusal.
    expect(hasButton(wrapper, 'Cancel')).toBe(true)
    expect(wrapper.find('button[type="submit"]').text()).toContain('Create trigger')
    wrapper.unmount()
  })

  it('does not offer a create submit with nothing to create', async () => {
    apiGet.mockResolvedValue(listing([]))
    const wrapper = mountSection()
    await flushPromises()
    await button(wrapper, 'New trigger').trigger('click')
    await nextTick()

    expect(wrapper.find('button[type="submit"]').attributes('disabled')).toBeDefined()
    wrapper.unmount()
  })
})
