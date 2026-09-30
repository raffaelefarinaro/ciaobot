// @vitest-environment jsdom

/**
 * The Categories panel.
 *
 * What only a mount can decide: that a shipped row is shown with its Built-in
 * chip and the vault's own note count, that a change to one row sends the whole
 * list (the API is a whole-list `PATCH`, so a per-row save would be a lie), that
 * a switch flipped off in the list is still off after a save made in the drawer,
 * that a builtin is offered no way to delete itself while a custom one is, and
 * that a refusal from the server reaches the person as the server's own sentence
 * rather than as a silent no-op.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import MemoryCategoriesPanel from '../MemoryCategoriesPanel.vue'
import { useProjectStore } from '../../stores/projects'
import { useEntityTypesStore, type EntityTypeRow, type EntityTypesResponse } from '../../stores/entityTypes'

const apiGet = vi.hoisted(() => vi.fn())
const apiPatch = vi.hoisted(() => vi.fn())

vi.mock('../../lib/api', () => ({
  api: { get: apiGet, post: vi.fn(), patch: apiPatch, del: vi.fn() },
}))

function row(overrides: Partial<EntityTypeRow> = {}): EntityTypeRow {
  return {
    id: 'person',
    label: 'Person',
    kind: 'entity',
    folder: 'People',
    description: 'A human the user knows or works with.',
    aliases: ['human'],
    stale_after_days: 90,
    enabled: true,
    builtin: true,
    core: false,
    hidden: false,
    note_count: 4,
    ...overrides,
  }
}

function body(types: EntityTypeRow[]): EntityTypesResponse {
  return { workspace: 'personal', vault: '/tmp/vault', types }
}

const STOCK = [
  row(),
  row({ id: 'place', label: 'Place', folder: 'Places', stale_after_days: 0, aliases: [], note_count: 11 }),
  row({
    id: 'customer', label: 'Customer', folder: 'Customers', description: 'Someone we do business with.',
    aliases: ['client'], stale_after_days: 0, builtin: false, note_count: 2,
  }),
]

const SYSTEM = [
  row({ id: 'project', label: 'Project', folder: 'Projects', core: true, note_count: 3 }),
  row({ id: 'workspace', label: 'Workspace', folder: 'Workspace', core: true, hidden: true, note_count: 9 }),
]

/** The shipped list only: `customer` is not in it, so the presets still offer it. */
const BUILTINS_ONLY = STOCK.slice(0, 2)

async function mountPanel(types: EntityTypeRow[] = STOCK) {
  apiGet.mockImplementation((url: string) =>
    Promise.resolve(url.includes('/api/memory/entity-types') ? body(types) : {}),
  )
  const wrapper = mount(MemoryCategoriesPanel, { attachTo: document.body })
  await flushPromises()
  await nextTick()
  return wrapper
}

function rowsOf(wrapper: ReturnType<typeof mount>) {
  return wrapper.findAll('.cat-row')
}

function drawerOf(wrapper: ReturnType<typeof mount>) {
  return wrapper.get('.cat-drawer')
}

describe('MemoryCategoriesPanel', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    apiGet.mockReset()
    apiPatch.mockReset()
    const store = useProjectStore()
    store.workspaces = [{ name: 'personal', vault_root: '/tmp/vault', default_provider: 'claude', gws_profile: '' }]
    store.activeWorkspace = 'personal'
  })

  afterEach(() => {
    document.body.innerHTML = ''
    vi.restoreAllMocks()
  })

  it('lists a core category with its switch locked, and leaves a hidden one out', async () => {
    const wrapper = await mountPanel([...STOCK, ...SYSTEM])
    const labels = rowsOf(wrapper).map((r) => r.find('.cat-name-btn').text())
    expect(labels).toContain('Project')
    expect(labels).not.toContain('Workspace')

    const project = rowsOf(wrapper).find((r) => r.find('.cat-name-btn').text() === 'Project')!
    const toggle = project.get('button[role="switch"]')
    expect((toggle.element as HTMLButtonElement).disabled).toBe(true)
    expect(toggle.attributes('aria-label')).toContain('required')

    // The heading counts what the page lists, not the hidden types.
    expect(wrapper.get('#cat-heading').text()).toBe('4 of 4 categories on')
  })

  it('reads the active workspace and lists every effective row with its chip and count', async () => {
    const wrapper = await mountPanel()
    expect(apiGet.mock.calls[0]![0]).toBe('/api/memory/entity-types?workspace=personal')

    const rows = rowsOf(wrapper)
    expect(rows).toHaveLength(3)
    // A shipped category says so, and a user one says that instead.
    expect(rows[0]!.get('.badge').text()).toBe('Built-in')
    expect(rows[2]!.get('.badge').text()).toBe('Custom')
    // The count is the vault's own, so a category the map has never heard of is
    // not reported as empty.
    expect(rows[0]!.get('.cat-num').text()).toBe('4')
    expect(rows[1]!.get('.cat-num').text()).toBe('11')
    expect(rows[0]!.text()).toContain('People')
    expect(rows[0]!.text()).toContain('90 days')
    expect(rows[1]!.text()).toContain('Default')
    wrapper.unmount()
  })

  it('keeps a disabled row listed and quiet, and its switch off', async () => {
    const wrapper = await mountPanel([row(), row({ id: 'log', label: 'Log', folder: '', enabled: false })])
    const off = rowsOf(wrapper)[1]!
    expect(off.classes()).toContain('cat-row--off')
    expect(off.get('.cat-switch').attributes('aria-checked')).toBe('false')
    // Still editable: turning it back on is the whole point of listing it.
    await off.get('.cat-name-btn').trigger('click')
    expect(drawerOf(wrapper).get<HTMLInputElement>('#cat-label').element.value).toBe('Log')
    wrapper.unmount()
  })

  it('marks a toggled row unsaved and sends the whole list on save', async () => {
    const wrapper = await mountPanel()
    // Nothing is pending before a change, so Save has nothing to say.
    expect(wrapper.get('.cat-save').attributes('disabled')).toBeDefined()
    // And with nothing to save it does not wear the primary either.
    expect(wrapper.get('.cat-save').classes()).not.toContain('cat-btn--primary')

    const swatch = rowsOf(wrapper)[0]!.get('.cat-switch')
    await swatch.trigger('click')
    await nextTick()
    expect(swatch.attributes('aria-checked')).toBe('false')
    expect(rowsOf(wrapper)[0]!.get('.cat-flag').text()).toBe('Unsaved')
    expect(wrapper.get('.cat-save').text()).toBe('Save changes')
    expect(wrapper.get('.cat-save').classes()).toContain('cat-btn--primary')

    const saved = [row({ enabled: false }), STOCK[1]!, STOCK[2]!]
    apiPatch.mockResolvedValue(body(saved))
    await wrapper.get('.cat-btn--primary').trigger('click')
    await flushPromises()
    await nextTick()

    expect(apiPatch).toHaveBeenCalledTimes(1)
    const [path, sent] = apiPatch.mock.calls[0]!
    expect(path).toBe('/api/memory/entity-types?workspace=personal')
    // The whole list, not the one row: the API takes a desired list, and a
    // payload naming one category would delete the other two.
    expect((sent as { types: Array<Record<string, unknown>> }).types).toHaveLength(3)
    expect((sent as { types: Array<Record<string, unknown>> }).types[0]).toMatchObject({ id: 'person', enabled: false })
    expect('note_count' in (sent as { types: Array<Record<string, unknown>> }).types[0]!).toBe(false)
    // Saved: the draft is the response, so nothing is pending any more.
    expect(rowsOf(wrapper)[0]!.find('.cat-flag').exists()).toBe(false)
    expect(wrapper.get('.cat-save').text()).toBe('Saved')
    wrapper.unmount()
  })

  it('a drawer save keeps an enable toggle the list is still holding', async () => {
    const wrapper = await mountPanel()
    // The switch owns `enabled` and the drawer owns none of it, so a pending
    // toggle has to survive an edit saved through the drawer: otherwise the
    // whole-list PATCH quietly turns the category back on.
    await rowsOf(wrapper)[0]!.get('.cat-switch').trigger('click')
    await nextTick()
    await rowsOf(wrapper)[0]!.get('.cat-name-btn').trigger('click')
    await nextTick()

    await drawerOf(wrapper).get('#cat-label').setValue('Human')
    apiPatch.mockResolvedValue(body([row({ label: 'Human', enabled: false }), STOCK[1]!, STOCK[2]!]))
    await drawerOf(wrapper).get('form').trigger('submit')
    await flushPromises()
    await nextTick()

    expect((apiPatch.mock.calls[0]![1] as { types: Array<Record<string, unknown>> }).types[0])
      .toMatchObject({ id: 'person', label: 'Human', enabled: false })
    // And the row reads as off, because the list shows the response the store
    // adopted rather than the submission.
    expect(rowsOf(wrapper)[0]!.get('.cat-switch').attributes('aria-checked')).toBe('false')
    wrapper.unmount()
  })

  it('opens the drawer with presets, and a new customer goes into the list', async () => {
    const wrapper = await mountPanel(BUILTINS_ONLY)
    await wrapper.findAll('.cat-btn').find(b => b.text() === 'Add category')!.trigger('click')
    await nextTick()

    const presets = drawerOf(wrapper).findAll('.cat-preset')
    expect(presets.map(p => p.text())).toEqual([
      'Customer', 'Country', 'Organisation', 'Event', 'Product', 'Document', 'Reference',
    ])
    // `place` ships as a builtin, so it is named as already there rather than
    // offered as an id the server would refuse as a duplicate.
    expect(drawerOf(wrapper).get('.cat-presets-note').text()).toContain('Place')

    await presets[0]!.trigger('click')
    const drawer = drawerOf(wrapper)
    expect((drawer.get('#cat-id').element as HTMLInputElement).value).toBe('customer')
    expect((drawer.get('#cat-label').element as HTMLInputElement).value).toBe('Customer')

    // The id is fixed once a category exists, and free while it is being added.
    expect((drawer.get('#cat-id').element as HTMLInputElement).readOnly).toBe(false)

    apiPatch.mockResolvedValue(body([...BUILTINS_ONLY, row({
      id: 'customer', label: 'Customer', folder: 'Customers',
      description: 'A person or company the user does business with. Use for accounts, deals and contacts.',
      aliases: [], stale_after_days: 0, builtin: false, note_count: 0,
    })]))
    await drawer.get('form').trigger('submit')
    await flushPromises()
    await nextTick()

    expect(apiPatch).toHaveBeenCalledTimes(1)
    const sent = (apiPatch.mock.calls[0]![1] as { types: Array<Record<string, unknown>> }).types
    expect(sent.map(t => t.id)).toEqual(['person', 'place', 'customer'])
    expect(sent[2]).toMatchObject({ id: 'customer', label: 'Customer', kind: 'entity', folder: 'Customers' })
    expect(wrapper.find('.cat-drawer').exists()).toBe(false)
    wrapper.unmount()
  })

  it('refuses a bad id and an empty label before a round trip', async () => {
    const wrapper = await mountPanel(BUILTINS_ONLY)
    await wrapper.findAll('.cat-btn').find(b => b.text() === 'Add category')!.trigger('click')
    await nextTick()
    const drawer = drawerOf(wrapper)

    // A pristine Add form is not an error: the disabled Save is the message,
    // and a red line on an untouched field would be noise.
    expect(drawer.find('#cat-label-error').exists()).toBe(false)
    expect(drawer.get('button[type="submit"]').attributes('disabled')).toBeDefined()

    await drawer.get('#cat-id').setValue('Customer')
    expect(drawer.get('#cat-id-error').text()).toMatch(/lower-case/)
    // An id the list already holds is caught here too, since the list is here.
    await drawer.get('#cat-id').setValue('person')
    expect(drawer.get('#cat-id-error').text()).toContain('already one of your categories')

    await drawer.get('#cat-id').setValue('customer')
    expect(drawer.find('#cat-id-error').exists()).toBe(false)
    expect(drawer.get('button[type="submit"]').attributes('disabled')).toBeDefined()

    await drawer.get('#cat-label').setValue('   ')
    expect(drawer.get('#cat-label-error').text()).toContain('needs a label')
    expect(drawer.get('button[type="submit"]').attributes('disabled')).toBeDefined()

    await drawer.get('#cat-label').setValue('Client')
    expect(drawer.find('#cat-label-error').exists()).toBe(false)
    expect(drawer.get('button[type="submit"]').attributes('disabled')).toBeUndefined()
    expect(apiPatch).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('edits a builtin without offering to delete it, and says why', async () => {
    const wrapper = await mountPanel()
    await rowsOf(wrapper)[0]!.get('.cat-name-btn').trigger('click')
    await nextTick()

    const drawer = drawerOf(wrapper)
    expect(drawer.get('h3').text()).toBe('Person')
    // The id is what a note writes, so a shipped one is not renamed from here.
    expect((drawer.get('#cat-id').element as HTMLInputElement).readOnly).toBe(true)
    expect(drawer.find('.cat-btn--danger').exists()).toBe(false)
    expect(drawer.text()).toContain('can be turned off, but not deleted')
    // The scope note is here so the stored folder/aliases/days are not read as
    // behaviour that already follows from them.
    expect(drawer.get('.cat-scope').text()).toContain('once that wiring lands')

    await drawer.get('#cat-label').setValue('Human')
    apiPatch.mockResolvedValue(body([row({ label: 'Human' }), STOCK[1]!, STOCK[2]!]))
    await drawer.get('form').trigger('submit')
    await flushPromises()
    await nextTick()

    expect((apiPatch.mock.calls[0]![1] as { types: Array<Record<string, unknown>> }).types[0])
      .toMatchObject({ id: 'person', label: 'Human' })
    expect(rowsOf(wrapper)[0]!.get('.cat-name-btn').text()).toBe('Human')
    wrapper.unmount()
  })

  it('deletes a custom category, and surfaces the refusal while its notes still use it', async () => {
    const wrapper = await mountPanel()
    await rowsOf(wrapper)[2]!.get('.cat-name-btn').trigger('click')
    await nextTick()

    apiPatch.mockRejectedValue(Object.assign(new Error('HTTP 400'), {
      payload: { error: 'refusing to delete a category its notes still use: customer (2 notes) — retype those notes first, or keep the category' },
    }))
    await drawerOf(wrapper).get('.cat-btn--danger').trigger('click')
    await flushPromises()
    await nextTick()

    // The list still holds it, and the server's sentence is on screen rather
    // than the row quietly disappearing.
    expect(rowsOf(wrapper)).toHaveLength(3)
    expect(drawerOf(wrapper).get('.cat-error').text()).toContain('customer (2 notes)')
    // The payload was the list without that one row — the only way the API can
    // express a delete.
    expect((apiPatch.mock.calls[0]![1] as { types: Array<Record<string, unknown>> }).types.map(t => t.id))
      .toEqual(['person', 'place'])

    // Once it goes through, the row is gone and the drawer closed — a form
    // left open on a deleted category would write it straight back.
    apiPatch.mockResolvedValue(body([STOCK[0]!, STOCK[1]!]))
    await drawerOf(wrapper).get('.cat-btn--danger').trigger('click')
    await flushPromises()
    await nextTick()
    expect(rowsOf(wrapper)).toHaveLength(2)
    expect(wrapper.find('.cat-drawer').exists()).toBe(false)
    wrapper.unmount()
  })

  it('shows the server\'s refusal on a save, with the edit still open', async () => {
    const wrapper = await mountPanel()
    await rowsOf(wrapper)[2]!.get('.cat-name-btn').trigger('click')
    await nextTick()
    await drawerOf(wrapper).get('#cat-folder').setValue('People')

    apiPatch.mockRejectedValue(Object.assign(new Error('HTTP 400'), {
      payload: { error: "two enabled categories share the folder 'People'" },
    }))
    await drawerOf(wrapper).get('form').trigger('submit')
    await flushPromises()
    await nextTick()

    expect(wrapper.find('.cat-drawer').exists()).toBe(true)
    expect(drawerOf(wrapper).get<HTMLInputElement>('#cat-folder').element.value).toBe('People')
    expect(drawerOf(wrapper).get('.cat-error').text()).toBe("two enabled categories share the folder 'People'")
    wrapper.unmount()
  })

  it('does not claim an empty vault when the first load failed', async () => {
    apiGet.mockRejectedValue(new Error('vault unavailable'))
    const wrapper = mount(MemoryCategoriesPanel, { attachTo: document.body })
    await flushPromises()
    await nextTick()

    expect(wrapper.get('.cat-failed').get('.cat-failed-text').text()).toBe('vault unavailable')
    expect(wrapper.findAll('.cat-row')).toHaveLength(0)
    expect(wrapper.text()).not.toContain('no categories yet')

    apiGet.mockResolvedValue(body(STOCK))
    await wrapper.get('.cat-failed').get('.cat-btn').trigger('click')
    await flushPromises()
    await nextTick()
    expect(rowsOf(wrapper)).toHaveLength(3)
    wrapper.unmount()
  })

  it('keeps the rows it has when a refresh fails, and offers a retry', async () => {
    const wrapper = await mountPanel()
    apiGet.mockRejectedValue(new Error('the vault is busy'))
    await useEntityTypesStore().reload('personal')
    await flushPromises()
    await nextTick()

    expect(rowsOf(wrapper)).toHaveLength(3)
    expect(wrapper.get('.cat-stale').text()).toContain('the vault is busy')
    expect(wrapper.get('.cat-stale').text()).toContain('as they were last read')
    wrapper.unmount()
  })

  it('closes the drawer on Escape and hands focus back to the row that opened it', async () => {
    const wrapper = await mountPanel()
    const opener = rowsOf(wrapper)[0]!.get('.cat-name-btn')
    ;(opener.element as HTMLElement).focus()
    await opener.trigger('click')
    await nextTick()
    await nextTick()

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flushPromises()
    await nextTick()
    expect(wrapper.find('.cat-drawer').exists()).toBe(false)
    expect(document.activeElement).toBe(opener.element)
    wrapper.unmount()
  })
})
