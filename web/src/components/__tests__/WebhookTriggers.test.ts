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

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import WebhookTriggers from '../WebhookTriggers.vue'
import { useProjectStore } from '../../stores/projects'
import { useWebhookStore } from '../../stores/webhooks'
import type { WebhookTrigger } from '../../lib/types'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())
const apiPatch = vi.hoisted(() => vi.fn())
const apiDel = vi.hoisted(() => vi.fn())
const writeClipboard = vi.hoisted(() => vi.fn(async (_text: string) => true))
const askConfirm = vi.hoisted(() => vi.fn(async () => true))

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
    mode: 'auto',
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
 * The row's delete is behind a Reka `DropdownMenu`, which portals its content to
 * `document.body` — stubbing Teleport would leave the menu permanently empty and
 * the only way to reach a destructive action untested. The helper below reads
 * the portal through `document`, which is where a reader's click lands anyway.
 */
function mountSection() {
  return mount(WebhookTriggers, { attachTo: document.body })
}

function button(wrapper: ReturnType<typeof mountSection>, label: string) {
  const found = wrapper.findAll('button').find(b => b.text() === label)
  if (!found) throw new Error(`no button labelled ${label}`)
  return found
}

function hasButton(wrapper: ReturnType<typeof mountSection>, label: string): boolean {
  return wrapper.findAll('button').some(b => b.text() === label)
}

function row(wrapper: ReturnType<typeof mountSection>, name: string) {
  const found = wrapper.findAll('.wh-row').find(r => r.text().includes(name))
  if (!found) throw new Error(`no row for ${name}`)
  return found
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

/** The open dialog, or `null`. Reka keeps both dialogs mounted, so open is state. */
function openDialog(wrapper: ReturnType<typeof mountSection>) {
  return wrapper.findAll('.wh-card').find(card => card.isVisible()) || null
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

  it('names the project or General, the mode, and when the row last changed', async () => {
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
      trigger({ trigger_id: 'trg_other', name: 'Plan mode one', project_id: 'proj_board', mode: 'plan' }),
    ]))
    const wrapper = mountSection()
    await flushPromises()

    // `project_id: null` is the workspace's General, a real destination and not
    // "unset" — a row reading "unset" would tell the user the trigger has none.
    expect(row(wrapper, 'New issue from GitHub').text()).toContain('General')
    expect(row(wrapper, 'New issue from GitHub').text()).toContain('Auto mode')
    expect(row(wrapper, 'New issue from GitHub').text()).toContain('updated')
    expect(row(wrapper, 'Plan mode one').text()).toContain('Board')
    expect(row(wrapper, 'Plan mode one').text()).toContain('Plan mode')
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

  it('sends the workspace, project, instructions and mode the form holds', async () => {
    apiGet.mockResolvedValue(listing([]))
    apiPost.mockResolvedValue({ trigger: trigger(), secret: SECRET })
    const wrapper = mountSection()
    await flushPromises()
    await openForm(wrapper)

    await wrapper.find('input[type="text"]').setValue('New issue from GitHub')
    await wrapper.find('textarea').setValue('Triage the issue and open a note.')
    const mode = wrapper.findAll('select').at(-1)!
    await mode.setValue('plan')
    await button(wrapper, 'Create trigger').trigger('click')
    await flushPromises()

    expect(apiPost).toHaveBeenCalledWith('/api/webhooks', {
      name: 'New issue from GitHub',
      workspace: 'personal',
      instructions: 'Triage the issue and open a note.',
      mode: 'plan',
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
    expect(wrapper.text()).toContain(SECRET)

    await button(wrapper, 'Done').trigger('click')
    await flushPromises()

    // Not hidden, gone: the engine only ever sends it once, so there is nothing
    // left to re-show and a later render must not be able to produce it again.
    expect(wrapper.text()).not.toContain(SECRET)
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
})

describe('rotating', () => {
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
    expect(wrapper.text()).not.toContain(SECRET)
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

  it('sends no create-only field, so mode and project cannot be smuggled in', async () => {
    apiGet.mockResolvedValue(listing([trigger({ mode: 'plan', project_id: 'proj_board' })]))
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
    expect(wrapper.text()).not.toContain(SECRET)
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