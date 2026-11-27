// @vitest-environment jsdom

/**
 * Memory · Unsure: one row per line of Workspace/Unsure.md, with Approve (tick),
 * Reject (remove), Discuss (a new chat) and Undo, all through
 * /api/memory/unsure, which names the line by a hash of its text.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Mock } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import MemoryUnsurePanel from '../MemoryUnsurePanel.vue'
import { api } from '../../lib/api'
import { useProjectStore } from '../../stores/projects'
import { useCareReportStore } from '../../stores/careReport'
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
    source: 'chat-1', date: '2026-11-20', raw: `- [ ] Item ${id}? More detail. — unsure because only one chat says it; source: chat-1; 2026-11-20`,
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

async function mountPanel() {
  const wrapper = mount(MemoryUnsurePanel, { attachTo: document.body })
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
    document.body.innerHTML = ''
    vi.restoreAllMocks()
  })

  it('shows a skeleton until the first read answers', async () => {
    apiGet.mockImplementation(() => new Promise(() => {}))
    const wrapper = mount(MemoryUnsurePanel)
    await flushPromises()
    expect(wrapper.find('.mu-skeleton').exists()).toBe(true)
    expect(wrapper.get('.mu').attributes('aria-busy')).toBe('true')
  })

  it('draws each line as a flat row: kind tag, first-sentence title, reason, source and date', async () => {
    answer(listOf([item('a'), item('b', { kind: 'observation' }), item('c', { checked: true })]))
    const wrapper = await mountPanel()
    const rows = wrapper.findAll('.mu-row')
    expect(rows).toHaveLength(3)
    expect(rows[0].get('.mu-kind').text()).toBe('fact')
    expect(rows[1].get('.mu-kind').text()).toBe('pattern')
    expect(rows[0].get('.mu-row-title').text()).toBe('Item a?')
    expect(rows[0].get('.mu-row-reason').text()).toBe('Unsure because only one chat says it')
    expect(rows[0].get('.mu-row-where').text()).toBe('chat-1 · 2026-11-20')
    expect(rows[0].attributes('tabindex')).toBe('0')
    expect(wrapper.findAll('.mu-group-title').map(h => h.text())).toEqual(['Waiting for you 2', 'Approved 1'])
    // The full line stays one click away.
    expect(rows[0].find('.mu-raw').exists()).toBe(false)
    await rows[0].get('.mu-disclosure').trigger('click')
    expect(rows[0].get('.mu-raw').text()).toBe(item('a').raw)
    expect(rows[0].get('.mu-disclosure').attributes('aria-expanded')).toBe('true')
  })

  it('Approve ticks that line, then the row says the next care run applies it and offers Undo', async () => {
    const a = item('a')
    answer(listOf([a]))
    apiPost.mockResolvedValueOnce(listOf([{ ...a, checked: true, id: 'f'.repeat(16) }]))
    const wrapper = await mountPanel()
    await button(wrapper, 'Approve: Item a?').trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith('/api/memory/unsure', { workspace: 'work', op: 'tick', id: a.id })
    expect(wrapper.get('.mu-row-state').text()).toContain('Approved: applied by the next care run.')
    expect(wrapper.get('.mu-notice').text()).toContain('The next care run applies it.')

    apiPost.mockResolvedValueOnce(listOf([a]))
    await button(wrapper, 'Undo approval: Item a?').trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenLastCalledWith('/api/memory/unsure', { workspace: 'work', op: 'untick', id: 'f'.repeat(16) })
    expect(wrapper.find('.mu-row-state').exists()).toBe(false)
  })

  it('Reject removes that line', async () => {
    const a = item('a')
    answer(listOf([a, item('b')]))
    apiPost.mockResolvedValueOnce(listOf([item('b')]))
    const wrapper = await mountPanel()
    await button(wrapper, 'Reject: Item a?').trigger('click')
    await flushPromises()
    expect(apiPost).toHaveBeenCalledWith('/api/memory/unsure', { workspace: 'work', op: 'remove', id: a.id })
    expect(wrapper.findAll('.mu-row-title').map(t => t.text())).toEqual(['Item b?'])
  })

  it('Discuss opens a new chat seeded with the full line and its source, with the list pinned', async () => {
    answer(listOf([item('a')]))
    const wrapper = await mountPanel()
    await button(wrapper, 'Discuss: Item a?').trigger('click')
    expect(apiPost).not.toHaveBeenCalled()
    const [, options] = startFileDiscussion.mock.calls[0]
    expect(options.path).toBe(PATH)
    expect(options.seed).toContain('Item a? More detail. — unsure because only one chat says it; source: chat-1')
    expect(options.seed).toContain('It came from chat-1.')
  })

  it('a category, repository or rule item cannot be applied unattended: Approve opens the chat instead', async () => {
    answer(listOf([item('r', { kind: 'repo', qualifier: '~/repos/app' }), item('u', { kind: 'rule' }), item('c', { kind: 'category' })]))
    const wrapper = await mountPanel()
    expect(wrapper.findAll('.mu-kind--chat')).toHaveLength(3)
    expect(wrapper.findAll('.mu-row-note')[0].text()).toContain('applied in a chat')
    await button(wrapper, 'Approve in a chat: Item r?').trigger('click')
    expect(apiPost).not.toHaveBeenCalled()
    expect(startFileDiscussion).toHaveBeenCalledTimes(1)
    // Reject still removes the line.
    apiPost.mockResolvedValueOnce(listOf([]))
    await button(wrapper, 'Reject: Item u?').trigger('click')
    expect(apiPost).toHaveBeenCalledWith('/api/memory/unsure', expect.objectContaining({ op: 'remove' }))
  })

  it('a stale page changes nothing: a 409 reads the list again and says why', async () => {
    const a = item('a')
    answer(listOf([a]))
    const wrapper = await mountPanel()
    apiPost.mockRejectedValueOnce(Object.assign(new Error('the list changed; reload it'), { status: 409 }))
    answer(listOf([item('z')]))
    await button(wrapper, 'Approve: Item a?').trigger('click')
    await flushPromises()
    expect(wrapper.get('.mu-notice').text()).toContain('The list changed since this page read it')
    expect(wrapper.findAll('.mu-row-title').map(t => t.text())).toEqual(['Item z?'])
  })

  it('Apply now runs care through its run-now path', async () => {
    answer(listOf([item('a', { checked: true })]))
    const wrapper = await mountPanel()
    const care = useCareReportStore()
    const run = vi.spyOn(care, 'runAgain').mockResolvedValue({ ok: true, message: 'Care started.' })
    await button(wrapper, 'Apply now: run care to apply Item a?').trigger('click')
    await flushPromises()
    expect(run).toHaveBeenCalled()
    expect(wrapper.get('.mu-notice').text()).toBe('Care started.')
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
    expect(failed.findAll('.mu-row')).toHaveLength(1)
  })

  it('Up and Down move between rows', async () => {
    answer(listOf([item('a'), item('b'), item('c')]))
    const wrapper = await mountPanel()
    const rows = wrapper.findAll<HTMLElement>('[data-unsure-row]')
    rows[0].element.focus()
    await rows[0].trigger('keydown', { key: 'ArrowDown' })
    expect(document.activeElement).toBe(rows[1].element)
    await rows[1].trigger('keydown', { key: 'ArrowUp' })
    expect(document.activeElement).toBe(rows[0].element)
  })

  it('keeps a malformed line visible: it can be rejected or discussed, never approved', async () => {
    answer(listOf([item('m', { malformed: true, kind: '', title: 'a plain bullet', raw: '- a plain bullet' })]))
    const wrapper = await mountPanel()
    expect(wrapper.get('.mu-kind').text()).toBe('line')
    await button(wrapper, 'Approve in a chat: a plain bullet').trigger('click')
    expect(startFileDiscussion).toHaveBeenCalled()
    expect(apiPost).not.toHaveBeenCalled()
  })
})
