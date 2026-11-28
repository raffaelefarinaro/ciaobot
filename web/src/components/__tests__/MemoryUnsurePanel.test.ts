// @vitest-environment jsdom

/**
 * Memory · Unsure as a review queue over the lines of Workspace/Unsure.md: the
 * list and the open item in full (desktop) or a swipeable card (phone);
 * Approve ticks, Dismiss removes, Skip moves on, acting moves to the next, and
 * the last action can be undone. Every write goes through /api/memory/unsure,
 * which names the line by a hash of its text.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Mock } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'
import MemoryUnsurePanel from '../MemoryUnsurePanel.vue'
import { api } from '../../lib/api'
import { useProjectStore } from '../../stores/projects'
import { useCareReportStore } from '../../stores/careReport'
import { useFileViewerStore } from '../../stores/fileViewer'
import type { UnsureItem, UnsureList } from '../../lib/unsureList'

const startFileDiscussion = vi.hoisted(() => vi.fn())
vi.mock('../../lib/fileDiscussion', () => ({ startFileDiscussion }))
vi.mock('../../lib/api', () => ({
  api: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), del: vi.fn() },
}))
const apiGet = api.get as unknown as Mock
const apiPost = api.post as unknown as Mock

const PATH = 'memory-vault/work/Workspace/Unsure.md'

function item(id: string, over: Partial<UnsureItem> = {}): UnsureItem {
  return {
    id: id.repeat(16).slice(0, 16), checked: false, kind: '', qualifier: '',
    title: `Item ${id}?`, text: `Item ${id}? More detail.`, reason: 'only one chat says it',
    source: 'chat-0123abcd', date: '2026-11-20', destination: '', proposal: '', raw: `- [ ] Item ${id}? More detail. — unsure because only one chat says it; source: chat-0123abcd; 2026-11-20`,
    malformed: false, ...over,
  }
}

function listOf(items: UnsureItem[]): UnsureList {
  const ticked = items.filter(i => i.checked).length
  return { workspace: 'work', path: PATH, found: true, items, counts: { waiting: items.length - ticked, ticked } }
}

function answer(list: UnsureList | Error) {
  apiGet.mockImplementation(async (url: string) => {
    if (url.startsWith('/api/memory/unsure')) {
      if (list instanceof Error) throw list
      return list
    }
    if (url.startsWith('/api/care-report')) {
      return { status: 'ok', workspace: 'work', steps: [], needs_you: [], counts: { total: 0, done: 0, blocked: 0, open: 0 }, schedule: { id: 'system-memory-curation@work', enabled: true, last_status: '', last_run_at: '' }, running: null }
    }
    return {}
  })
}

const mounted: { unmount: () => void }[] = []

async function mountPanel() {
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:any(.*)*', component: { template: '<div />' } }] })
  const wrapper = mount(MemoryUnsurePanel, { attachTo: document.body, global: { plugins: [router] } })
  mounted.push(wrapper)
  await flushPromises()
  return wrapper
}

function button(wrapper: Awaited<ReturnType<typeof mountPanel>>, name: string) {
  const found = wrapper.findAll('button').find(b => b.attributes('aria-label') === name || b.text() === name)
  if (!found) throw new Error(`no button ${name}`)
  return found
}

describe('MemoryUnsurePanel', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    apiGet.mockReset()
    apiPost.mockReset()
    startFileDiscussion.mockReset()
    const store = useProjectStore()
    store.workspaces = [{ name: 'work', vault_root: 'memory-vault/work', default_provider: 'claude' }]
    store.activeWorkspace = 'work'
  })

  afterEach(() => {
    for (const wrapper of mounted.splice(0)) {
      try { wrapper.unmount() } catch { /* already unmounted by the test */ }
    }
    document.body.innerHTML = ''
    vi.unstubAllGlobals()
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  const titles = (wrapper: Awaited<ReturnType<typeof mountPanel>>) => wrapper.findAll('.mu-row-title').map(t => t.text())
  const detailTitle = (wrapper: Awaited<ReturnType<typeof mountPanel>>) => wrapper.get('#mu-detail-title').text()
  const live = () => document.querySelector('[data-unsure-live]')?.textContent ?? ''
  function press(key: string, init: KeyboardEventInit = {}, target: EventTarget = document.body): KeyboardEvent {
    const event = new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...init })
    target.dispatchEvent(event)
    return event
  }

  it('shows a skeleton until the first read answers', async () => {
    apiGet.mockImplementation(() => new Promise(() => {}))
    const wrapper = await mountPanel()
    expect(wrapper.find('.mu-skeleton').exists()).toBe(true)
    expect(wrapper.get('.mu').attributes('aria-busy')).toBe('true')
  })

  it('lists the waiting items with their reason and chats, and opens the first one in full', async () => {
    const full = item('a', {
      title: 'Should I remember that Mo now works at Acme?',
      text: 'Should I remember that Mo now works at Acme? He said so. "Mo works at Acme."',
      proposal: 'Mo works at Acme.', destination: 'People/Mo.md', reason: 'the [Mo note](../People/Mo.md) says otherwise',
    })
    answer(listOf([full, item('b', { kind: 'observation' }), item('c', { checked: true })]))
    const wrapper = await mountPanel()
    const rows = wrapper.findAll('[data-unsure-row]')
    expect(rows).toHaveLength(3)
    expect(rows[0].get('.mu-kind').text()).toBe('fact')
    expect(rows[1].get('.mu-kind').text()).toBe('pattern')
    expect(rows[0].get('.mu-row-sub').text()).toBe('Unsure because the Mo note says otherwise · chat-0123abcd')
    expect(rows[0].attributes('aria-selected')).toBe('true')
    expect(rows[0].attributes('tabindex')).toBe('0')
    expect(rows[1].attributes('tabindex')).toBe('-1')
    expect(wrapper.get('[data-unsure-count]').text()).toBe('1 of 2')
    expect(wrapper.get('.mu-group-title').text()).toBe('Approved, not saved yet 1')

    // The detail: the whole title, the words after it, why, where from, and what approving saves where.
    expect(detailTitle(wrapper)).toBe('Should I remember that Mo now works at Acme?')
    expect(wrapper.get('.mud-more').text()).toBe('He said so.')
    expect(wrapper.get('.mud-reason').text()).toBe('the Mo note says otherwise')
    expect(wrapper.get('.mud-meta').text()).toContain('Found Nov 20')
    expect(wrapper.get('.mud-meta').text()).toContain('1 chat')
    expect(wrapper.get('a.mus-chip').attributes('href')).toBe('/chat/chat-0123abcd')
    expect(wrapper.get('.mud-effect-line').text()).toBe('Mo works at Acme.')
    expect(wrapper.get('.mud-effect-where').text()).toContain('Saved to People/Mo.md')
    expect(wrapper.get('.mud-effect-how').text()).toBe('The next Workspace care run writes it there and takes the line off this list.')
  })

  it('opens the note a reason links to and the destination note in the file viewer', async () => {
    answer(listOf([item('a', { reason: 'the [Mo note](../People/Mo.md) says otherwise', proposal: 'Mo works at Acme.', destination: 'People/Mo.md' })]))
    const wrapper = await mountPanel()
    const viewer = useFileViewerStore()
    const open = vi.spyOn(viewer, 'open').mockResolvedValue(undefined as never)
    await wrapper.get('.mud-reason button').trigger('click')
    expect(open).toHaveBeenLastCalledWith('memory-vault/work/People/Mo.md')
    await wrapper.get('.mud-effect-where button').trigger('click')
    expect(open).toHaveBeenLastCalledWith('memory-vault/work/People/Mo.md')
  })

  it('shows no line and no destination for an item that does not carry them', async () => {
    answer(listOf([item('a')]))
    const wrapper = await mountPanel()
    expect(wrapper.find('.mud-effect-line').exists()).toBe(false)
    expect(wrapper.find('.mud-effect-where').exists()).toBe(false)
    expect(wrapper.get('.mud-effect-how').text()).toContain('This line does not say where.')
  })

  it('a click on a row opens that item', async () => {
    answer(listOf([item('a'), item('b')]))
    const wrapper = await mountPanel()
    await wrapper.findAll('[data-unsure-row]')[1].trigger('click')
    expect(detailTitle(wrapper)).toBe('Item b?')
    expect(wrapper.get('[data-unsure-count]').text()).toBe('2 of 2')
  })

  it('Approve ticks the line, moves to the next item, says so, and Undo unticks it', async () => {
    const [a, b] = [item('a'), item('b')]
    answer(listOf([a, b]))
    const tickedA = { ...a, checked: true, id: 'f'.repeat(16), raw: a.raw.replace('- [ ]', '- [x]') }
    apiPost.mockResolvedValueOnce(listOf([tickedA, b]))
    const wrapper = await mountPanel()
    await button(wrapper, 'Approve: Item a?').trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith('/api/memory/unsure', { workspace: 'work', op: 'tick', id: a.id })
    expect(detailTitle(wrapper)).toBe('Item b?')
    expect(wrapper.get('[data-unsure-notice]').text()).toContain('Approved “Item a?”. The next Workspace care run saves it.')
    await flushPromises()
    expect(live()).toBe('Approved “Item a?”. The next Workspace care run saves it. Now 1 of 1: “Item b?”.')

    apiPost.mockResolvedValueOnce(listOf([a, b]))
    await button(wrapper, 'Undo').trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenLastCalledWith('/api/memory/unsure', { workspace: 'work', op: 'untick', id: 'f'.repeat(16) })
    expect(detailTitle(wrapper)).toBe('Item a?')
    expect(wrapper.find('.mu-undo').exists()).toBe(false)
  })

  it('D dismisses the open item and Undo puts the exact line back before its neighbour', async () => {
    const [a, b, c] = [item('a'), item('b'), item('c')]
    answer(listOf([a, b, c]))
    const wrapper = await mountPanel()
    await wrapper.findAll('[data-unsure-row]')[1].trigger('click')
    apiPost.mockResolvedValueOnce(listOf([a, c]))
    press('d')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith('/api/memory/unsure', { workspace: 'work', op: 'remove', id: b.id })
    expect(detailTitle(wrapper)).toBe('Item c?')
    expect(wrapper.get('[data-unsure-notice]').text()).toContain('Dismissed “Item b?”.')

    apiPost.mockResolvedValueOnce(listOf([a, b, c]))
    await button(wrapper, 'Undo').trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenLastCalledWith('/api/memory/unsure', { workspace: 'work', op: 'restore', line: b.raw, before: c.id })
    expect(detailTitle(wrapper)).toBe('Item b?')
    expect(live()).toContain('“Item b?” is back on the list.')
  })

  it('A approves, S skips without writing, and the arrows move', async () => {
    const [a, b, c] = [item('a'), item('b'), item('c')]
    answer(listOf([a, b, c]))
    const wrapper = await mountPanel()
    press('ArrowDown')
    await flushPromises()
    expect(detailTitle(wrapper)).toBe('Item b?')
    expect(live()).toBe('Now 2 of 3: “Item b?”.')
    press('ArrowUp')
    await flushPromises()
    expect(detailTitle(wrapper)).toBe('Item a?')
    press('s')
    await flushPromises()
    expect(apiPost).not.toHaveBeenCalled()
    expect(detailTitle(wrapper)).toBe('Item b?')
    expect(live()).toBe('Skipped. Now 2 of 3: “Item b?”.')
    apiPost.mockResolvedValueOnce(listOf([a, { ...b, checked: true }, c]))
    press('a')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith('/api/memory/unsure', { workspace: 'work', op: 'tick', id: b.id })
    expect(detailTitle(wrapper)).toBe('Item c?')
  })

  it('the keys do nothing in a text field, with a modifier, mid-composition or under a dialog', async () => {
    answer(listOf([item('a'), item('b')]))
    const wrapper = await mountPanel()
    const field = document.createElement('input')
    document.body.appendChild(field)
    field.focus()
    expect(press('a', {}, field).defaultPrevented).toBe(false)
    expect(press('ArrowDown', {}, field).defaultPrevented).toBe(false)
    expect(press('d', { metaKey: true }).defaultPrevented).toBe(false)
    expect(press('d', { shiftKey: true }).defaultPrevented).toBe(false)
    expect(press('a', { isComposing: true }).defaultPrevented).toBe(false)
    const dialog = document.createElement('div')
    dialog.setAttribute('aria-modal', 'true')
    document.body.appendChild(dialog)
    expect(press('d').defaultPrevented).toBe(false)
    dialog.remove()
    await flushPromises()
    expect(apiPost).not.toHaveBeenCalled()
    expect(detailTitle(wrapper)).toBe('Item a?')
  })

  it('arrows on a focused row move focus with the selection', async () => {
    answer(listOf([item('a'), item('b'), item('c')]))
    const wrapper = await mountPanel()
    const rows = () => wrapper.findAll<HTMLElement>('[data-unsure-row]')
    rows()[0].element.focus()
    await rows()[0].trigger('keydown', { key: 'ArrowDown' })
    await flushPromises()
    expect(document.activeElement).toBe(rows()[1].element)
    expect(rows()[1].attributes('aria-selected')).toBe('true')
    await rows()[1].trigger('keydown', { key: 'ArrowUp' })
    await flushPromises()
    expect(document.activeElement).toBe(rows()[0].element)
  })

  it('after acting from a row, focus lands on the row that took its place, then on the empty line', async () => {
    const [a, b] = [item('a'), item('b')]
    answer(listOf([a, b]))
    const wrapper = await mountPanel()
    ;(wrapper.get('[data-unsure-row]').element as HTMLElement).focus()
    apiPost.mockResolvedValueOnce(listOf([b]))
    press('d', {}, document.activeElement as HTMLElement)
    await flushPromises()
    expect((document.activeElement as HTMLElement).matches('[data-unsure-row]')).toBe(true)
    expect(document.activeElement?.textContent).toContain('Item b?')

    // A button that is still there keeps focus.
    const dismiss = button(wrapper, 'Dismiss: Item b?')
    apiPost.mockResolvedValueOnce(listOf([]))
    ;(dismiss.element as HTMLElement).focus()
    await dismiss.trigger('click')
    await flushPromises()
    expect(document.activeElement?.textContent).toContain('Nothing is waiting.')
  })

  it('a category, repository or rule item opens a chat to approve, from the button or the key', async () => {
    answer(listOf([item('r', { kind: 'repo', qualifier: '~/repos/app' }), item('u', { kind: 'rule' })]))
    const wrapper = await mountPanel()
    expect(wrapper.findAll('.mu-row .mu-kind--chat')).toHaveLength(2)
    expect(wrapper.get('.mud-effect-where').text()).toContain('~/repos/app/AGENTS.md')
    await button(wrapper, 'Approve in a chat: Item r?').trigger('click')
    expect(startFileDiscussion).toHaveBeenCalledTimes(1)
    press('a')
    await flushPromises()
    expect(startFileDiscussion).toHaveBeenCalledTimes(2)
    expect(apiPost).not.toHaveBeenCalled()
  })

  it('Discuss opens a new chat seeded with the full line and its source, with the list pinned', async () => {
    answer(listOf([item('a')]))
    const wrapper = await mountPanel()
    await button(wrapper, 'Discuss: Item a?').trigger('click')
    expect(apiPost).not.toHaveBeenCalled()
    const [, options] = startFileDiscussion.mock.calls[0]
    expect(options.path).toBe(PATH)
    expect(options.seed).toContain('Item a? More detail. — unsure because only one chat says it; source: chat-0123abcd')
  })

  it('a stale page changes nothing: a 409 reads the list again and says why', async () => {
    answer(listOf([item('a')]))
    const wrapper = await mountPanel()
    apiPost.mockRejectedValueOnce(Object.assign(new Error('the list changed; reload it'), { status: 409, payload: { code: 'line_changed' } }))
    answer(listOf([item('z')]))
    await button(wrapper, 'Approve: Item a?').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-unsure-notice]').text()).toContain('The list changed since this page read it')
    expect(titles(wrapper)).toEqual(['Item z?'])
  })

  it('while care runs, Approve, Dismiss and the keys wait; Skip and a chat approval still work', async () => {
    answer(listOf([item('a'), item('b'), item('r', { kind: 'repo' })]))
    const wrapper = await mountPanel()
    const care = useCareReportStore()
    care.report = { ...(care.report as object), running: { since: '', step: 'unsure', index: 1, total: 6 } } as never
    await flushPromises()
    expect(wrapper.text()).toContain('Workspace care is running and may change this list.')
    expect(button(wrapper, 'Approve: Item a?').attributes('disabled')).toBeDefined()
    expect(button(wrapper, 'Dismiss: Item a?').attributes('disabled')).toBeDefined()
    expect(button(wrapper, 'Skip: Item a?').attributes('disabled')).toBeUndefined()
    press('a')
    await flushPromises()
    expect(apiPost).not.toHaveBeenCalled()
    expect(live()).toBe('Workspace care is running; try again when it finishes.')
    await wrapper.findAll('[data-unsure-row]')[2].trigger('click')
    expect(button(wrapper, 'Approve in a chat: Item r?').attributes('disabled')).toBeUndefined()
  })

  it('an approved item says when it is saved, with Apply now and Undo approval', async () => {
    const done = item('a', { checked: true, raw: '- [x] Item a? More detail. — unsure because only one chat says it; source: chat-0123abcd; 2026-11-20' })
    answer(listOf([done]))
    const wrapper = await mountPanel()
    expect(wrapper.get('.mu-detail-actions').text()).toContain('Approved: saved by the next Workspace care run.')
    const care = useCareReportStore()
    const run = vi.spyOn(care, 'runAgain').mockResolvedValue({ ok: true, message: 'Care started.' })
    await button(wrapper, 'Apply now: run care to save Item a?').trigger('click')
    await flushPromises()
    expect(run).toHaveBeenCalled()
    expect(wrapper.get('[data-unsure-notice]').text()).toBe('Care started.')
    apiPost.mockResolvedValueOnce(listOf([item('a')]))
    await button(wrapper, 'Undo approval: Item a?').trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenLastCalledWith('/api/memory/unsure', { workspace: 'work', op: 'untick', id: done.id })
    expect(button(wrapper, 'Approve: Item a?').exists()).toBe(true)
  })

  it('reads the list again when the page opens on a workspace it already showed', async () => {
    answer(listOf([item('a')]))
    const first = await mountPanel()
    first.unmount()
    const reads = () => apiGet.mock.calls.filter(([url]) => String(url).startsWith('/api/memory/unsure')).length
    expect(reads()).toBe(1)
    answer(listOf([item('a'), item('n')]))
    const again = await mountPanel()
    expect(reads()).toBe(2)
    expect(again.findAll('[data-unsure-row]')).toHaveLength(2)
  })

  it('reads the list again when the tab comes back', async () => {
    answer(listOf([item('a')]))
    const again = await mountPanel()
    answer(listOf([item('a'), item('n'), item('m')]))
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' })
    document.dispatchEvent(new Event('visibilitychange'))
    await flushPromises()
    expect(again.findAll('[data-unsure-row]')).toHaveLength(3)
  })

  it('says so when nothing is waiting, and offers a retry when the read fails', async () => {
    answer(listOf([]))
    const empty = await mountPanel()
    expect(empty.text()).toContain('Nothing is waiting. Care adds a line here only when it is not sure enough to write something down.')
    empty.unmount()

    setActivePinia(createPinia())
    useProjectStore().activeWorkspace = 'work'
    answer(new Error('HTTP 500'))
    const failed = await mountPanel()
    expect(failed.get('[role="alert"]').text()).toContain('Could not read the list. HTTP 500')
    answer(listOf([item('a')]))
    await button(failed, 'Try again').trigger('click')
    await flushPromises()
    expect(failed.findAll('[data-unsure-row]')).toHaveLength(1)
  })

  it('keeps a malformed line visible: it can be dismissed or discussed, and Approve opens a chat', async () => {
    answer(listOf([item('m', { malformed: true, kind: '', title: 'a plain bullet', raw: '- a plain bullet' })]))
    const wrapper = await mountPanel()
    expect(wrapper.get('.mu-kind').text()).toBe('line')
    await button(wrapper, 'Approve in a chat: a plain bullet').trigger('click')
    expect(startFileDiscussion).toHaveBeenCalled()
    expect(apiPost).not.toHaveBeenCalled()
  })

  describe('on a phone-narrow pane', () => {
    beforeEach(() => {
      // The pane measures 390px wide: the page draws the card deck.
      vi.stubGlobal('ResizeObserver', class {
        constructor(private readonly callback: ResizeObserverCallback) {}
        observe(): void { this.callback([{ contentRect: { width: 390 } } as ResizeObserverEntry], this as unknown as ResizeObserver) }
        disconnect(): void {}
        unobserve(): void {}
      })
    })

    const card = (wrapper: Awaited<ReturnType<typeof mountPanel>>) => wrapper.get('[data-unsure-card]')
    /** jsdom has no PointerEvent: a MouseEvent of the pointer type, carrying a touch pointer's fields. */
    async function pointer(el: Element, type: string, x: number, y: number, id = 7): Promise<void> {
      const event = new MouseEvent(type, { clientX: x, clientY: y, bubbles: true, cancelable: true })
      Object.defineProperties(event, { isPrimary: { value: true }, pointerId: { value: id }, pointerType: { value: 'touch' } })
      el.dispatchEvent(event)
      await nextTick()
    }
    async function drag(wrapper: Awaited<ReturnType<typeof mountPanel>>, dx: number, dy = 0): Promise<void> {
      const el = card(wrapper).element
      await pointer(el, 'pointerdown', 100, 100)
      await pointer(el, 'pointermove', 100 + dx / 2, 100 + dy / 2)
      await pointer(el, 'pointermove', 100 + dx, 100 + dy)
      await pointer(el, 'pointerup', 100 + dx, 100 + dy)
    }

    it('shows one card with its place, the swipe hints and the two buttons', async () => {
      answer(listOf([item('a'), item('b'), item('c')]))
      const wrapper = await mountPanel()
      expect(wrapper.find('.mu-queue').exists()).toBe(false)
      expect(wrapper.get('[data-unsure-count]').text()).toBe('1 of 3')
      expect(card(wrapper).text()).toContain('Item a?')
      expect(wrapper.findAll('.mudk-shadow')).toHaveLength(2)
      expect(button(wrapper, 'Dismiss: Item a?').exists()).toBe(true)
      expect(button(wrapper, 'Approve: Item a?').exists()).toBe(true)
    })

    it('a swipe right past the threshold approves; the card flies off first', async () => {
      vi.useFakeTimers()
      const [a, b] = [item('a'), item('b')]
      answer(listOf([a, b]))
      const wrapper = await mountPanel()
      apiPost.mockResolvedValueOnce(listOf([{ ...a, checked: true }, b]))
      await drag(wrapper, 160)
      expect(card(wrapper).classes()).toContain('mudk-card--flying')
      expect(apiPost).not.toHaveBeenCalled()
      await vi.advanceTimersByTimeAsync(200)
      await flushPromises()
      expect(apiPost).toHaveBeenCalledWith('/api/memory/unsure', { workspace: 'work', op: 'tick', id: a.id })
      expect(card(wrapper).text()).toContain('Item b?')
      expect(card(wrapper).attributes('style') ?? '').not.toContain('translateX')
    })

    it('a swipe left dismisses at once when motion is reduced, with no tilt while dragging', async () => {
      vi.stubGlobal('matchMedia', (query: string) => ({ matches: query.includes('reduce'), addEventListener() {}, removeEventListener() {} }))
      const [a, b] = [item('a'), item('b')]
      answer(listOf([a, b]))
      const wrapper = await mountPanel()
      const el = card(wrapper)
      await pointer(el.element, 'pointerdown', 200, 100, 3)
      await pointer(el.element, 'pointermove', 150, 100, 3)
      expect(el.attributes('style')).toContain('translateX(-50px)')
      expect(el.attributes('style')).not.toContain('rotate')
      apiPost.mockResolvedValueOnce(listOf([b]))
      await pointer(el.element, 'pointermove', 40, 100, 3)
      await pointer(el.element, 'pointerup', 40, 100, 3)
      await flushPromises()
      expect(apiPost).toHaveBeenCalledWith('/api/memory/unsure', { workspace: 'work', op: 'remove', id: a.id })
    })

    it('a short drag springs back and a vertical one is a scroll: nothing is written', async () => {
      answer(listOf([item('a'), item('b')]))
      const wrapper = await mountPanel()
      // Short of the threshold and too short to be a fling, however fast it was.
      await drag(wrapper, 30)
      await drag(wrapper, 160, 400)
      await flushPromises()
      expect(apiPost).not.toHaveBeenCalled()
      expect(card(wrapper).attributes('style') ?? '').not.toContain('translateX')
      expect(card(wrapper).text()).toContain('Item a?')
    })

    it('the buttons under the card decide too, and Skip shows the next card', async () => {
      vi.stubGlobal('matchMedia', (query: string) => ({ matches: query.includes('reduce'), addEventListener() {}, removeEventListener() {} }))
      const [a, b, c] = [item('a'), item('b'), item('c')]
      answer(listOf([a, b, c]))
      const wrapper = await mountPanel()
      await button(wrapper, 'Skip: Item a?').trigger('click')
      expect(wrapper.get('[data-unsure-count]').text()).toBe('2 of 3')
      apiPost.mockResolvedValueOnce(listOf([a, c]))
      await button(wrapper, 'Dismiss: Item b?').trigger('click')
      await flushPromises()
      expect(apiPost).toHaveBeenCalledWith('/api/memory/unsure', { workspace: 'work', op: 'remove', id: b.id })
      expect(card(wrapper).text()).toContain('Item c?')
    })
  })
})
