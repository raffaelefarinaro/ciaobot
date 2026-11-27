// @vitest-environment jsdom

import { afterEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import ChatSearchDialog from '../ChatSearchDialog.vue'

const { post } = vi.hoisted(() => ({ post: vi.fn() }))
vi.mock('../../lib/api', () => ({ api: { post: (...args: unknown[]) => post(...args) } }))

async function settle() {
  await nextTick()
  await nextTick()
}

async function searchFor(q: string) {
  const input = document.querySelector<HTMLInputElement>('#chat-search-input')!
  input.value = q
  input.dispatchEvent(new Event('input', { bubbles: true }))
  await nextTick()
  await vi.advanceTimersByTimeAsync(250)
  await settle()
  return input
}

describe('ChatSearchDialog', () => {
  Element.prototype.scrollIntoView = vi.fn()

  afterEach(() => { document.body.innerHTML = ''; vi.clearAllMocks(); vi.useRealTimers() })

  it('posts scoped queries in the body and renders matches as literal text', async () => {
    vi.useFakeTimers()
    post.mockResolvedValue({ items: [{ chat_id: 'c1', title: '<img src=x>', project_title: 'Project', archived: false, snippet: '<script>match</script>' }], next_cursor: null, skipped_large_transcripts: false })
    const wrapper = mount(ChatSearchDialog, { attachTo: document.body, props: { open: true, workspace: 'work' } })
    await settle()
    await vi.advanceTimersByTimeAsync(20)
    expect(document.activeElement).toBe(document.querySelector('#chat-search-input'))
    const input = document.querySelector<HTMLInputElement>('#chat-search-input')!
    input.value = 'match'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()
    await vi.advanceTimersByTimeAsync(250)
    expect(post).toHaveBeenCalledWith('/api/chats/search', { workspace: 'work', q: 'match' })
    await settle()
    expect(document.querySelector('.chat-search-result-title')?.textContent).toBe('<img src=x>')
    expect(document.querySelector('.chat-search-snippet')?.textContent).toBe('<script>match</script>')
    expect(document.querySelector('img, script')).toBeNull()
    wrapper.unmount()
  })

  it('restores focus to the invoking control after dismissal', async () => {
    const opener = document.createElement('button')
    opener.textContent = 'Search chats'
    document.body.append(opener)
    opener.focus()
    const wrapper = mount(ChatSearchDialog, { attachTo: document.body, props: { open: true, workspace: 'work' } })
    await settle()

    document.querySelector<HTMLButtonElement>('.chat-search-close')!.click()
    await wrapper.setProps({ open: false })
    await settle()
    expect(document.activeElement).toBe(opener)
    wrapper.unmount()
  })

  it('keeps focus with the destination when selecting a result closes the dialog', async () => {
    vi.useFakeTimers()
    post.mockResolvedValueOnce({ items: [
      { chat_id: 'chosen', title: 'Chosen chat', project_title: 'Project', archived: false, snippet: 'match' },
    ], next_cursor: null, skipped_large_transcripts: false })
    const opener = document.createElement('button')
    const destination = document.createElement('button')
    document.body.append(opener, destination)
    opener.focus()
    const wrapper = mount(ChatSearchDialog, {
      attachTo: document.body,
      props: { open: true, workspace: 'work' },
    })
    await settle()
    await vi.advanceTimersByTimeAsync(20)
    const input = document.querySelector<HTMLInputElement>('#chat-search-input')!
    input.value = 'match'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()
    await vi.advanceTimersByTimeAsync(250)
    await settle()
    document.querySelector<HTMLButtonElement>('.chat-search-result')!.click()
    await wrapper.setProps({ open: false })
    destination.focus()
    await settle()
    expect(document.activeElement).toBe(destination)
    wrapper.unmount()
  })

  it('discards late responses after query or workspace changes and fetches another page on demand', async () => {
    vi.useFakeTimers()
    let resolveOld!: (value: unknown) => void
    post.mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve }))
    post.mockResolvedValueOnce({ items: [
      { chat_id: 'active', title: 'Active match', project_title: 'Project', archived: false, snippet: 'literal' },
      { chat_id: 'archived', title: 'Archived match', project_title: 'Project', archived: true, snippet: 'saved text' },
    ], next_cursor: 'next', skipped_large_transcripts: false })
    post.mockResolvedValueOnce({ items: [
      { chat_id: 'next', title: 'Next result', project_title: 'Project', archived: false, snippet: 'more' },
    ], next_cursor: null, skipped_large_transcripts: false })
    post.mockResolvedValueOnce({ items: [
      { chat_id: 'archived-other', title: 'Archived in other workspace', project_title: 'Other', archived: true, snippet: 'saved' },
    ], next_cursor: null, skipped_large_transcripts: false })
    const wrapper = mount(ChatSearchDialog, { attachTo: document.body, props: { open: true, workspace: 'work' } })
    await settle()
    await vi.advanceTimersByTimeAsync(20)
    const input = document.querySelector<HTMLInputElement>('#chat-search-input')!
    input.value = 'old query'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()
    await vi.advanceTimersByTimeAsync(250)
    input.value = 'new query'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()
    expect(document.querySelector('.chat-search-results')).toBeNull()
    await vi.advanceTimersByTimeAsync(250)
    resolveOld({ items: [{ chat_id: 'stale', title: 'Stale', project_title: 'Old', archived: false, snippet: '' }], next_cursor: null, skipped_large_transcripts: false })
    await nextTick()
    await nextTick()
    expect(document.querySelector('.chat-search-result-title')?.textContent).toBe('Active match')
    expect(post.mock.calls[1]).toEqual(['/api/chats/search', { workspace: 'work', q: 'new query' }])
    await vi.advanceTimersByTimeAsync(0)
    const more = document.querySelector<HTMLButtonElement>('.chat-search-more')!
    more.click()
    await nextTick()
    await nextTick()
    expect(post.mock.calls[2]).toEqual(['/api/chats/search', { workspace: 'work', q: 'new query', cursor: 'next' }])
    expect([...document.querySelectorAll('.chat-search-result-title')].map(node => node.textContent)).toEqual([
      'Active match', 'Archived match', 'Next result',
    ])
    expect(document.body.textContent).toContain('Archived')
    await wrapper.setProps({ workspace: 'other' })
    await nextTick()
    expect(document.querySelector('.chat-search-results')).toBeNull()
    await vi.advanceTimersByTimeAsync(250)
    await nextTick()
    expect(post.mock.calls[3]).toEqual(['/api/chats/search', { workspace: 'other', q: 'new query' }])
    expect(document.querySelector('.chat-search-result-title')?.textContent).toBe('Archived in other workspace')
    document.querySelector<HTMLButtonElement>('.chat-search-result')!.click()
    expect(wrapper.emitted('select')?.at(-1)).toEqual(['archived-other'])
    wrapper.unmount()
  })

  it('announces empty and failed states and lets the user retry', async () => {
    vi.useFakeTimers()
    post.mockResolvedValueOnce({ items: [], next_cursor: null, skipped_large_transcripts: false })
    post.mockRejectedValueOnce(new Error('offline'))
    post.mockResolvedValueOnce({ items: [], next_cursor: null, skipped_large_transcripts: true })
    const wrapper = mount(ChatSearchDialog, { attachTo: document.body, props: { open: true, workspace: 'work' } })
    await settle()
    await vi.advanceTimersByTimeAsync(20)
    const input = document.querySelector<HTMLInputElement>('#chat-search-input')!
    input.value = 'none'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()
    await vi.advanceTimersByTimeAsync(250)
    await nextTick()
    expect(document.querySelector('[role="status"]')?.textContent).toBe('No matching chats.')
    input.value = 'retry'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()
    await vi.advanceTimersByTimeAsync(250)
    await nextTick()
    expect(document.querySelector('[role="status"]')?.textContent).toBe('Search failed. Try again.')
    document.querySelector<HTMLButtonElement>('.chat-search-more')!.click()
    await nextTick()
    await nextTick()
    expect(post).toHaveBeenCalledTimes(3)
    expect(document.body.textContent).toContain('Some large transcripts were skipped')
    wrapper.unmount()
  })

  it('retains skipped transcript status across pages and retries a failed more request with its cursor', async () => {
    vi.useFakeTimers()
    post.mockResolvedValueOnce({ items: [
      { chat_id: 'first', title: 'First', project_title: 'Project', archived: false, snippet: 'one' },
    ], next_cursor: 'cursor-2', skipped_large_transcripts: true })
    post.mockRejectedValueOnce(new Error('offline'))
    post.mockResolvedValueOnce({ items: [
      { chat_id: 'second', title: 'Second', project_title: 'Project', archived: true, snippet: 'two' },
    ], next_cursor: null, skipped_large_transcripts: false })
    const wrapper = mount(ChatSearchDialog, { attachTo: document.body, props: { open: true, workspace: 'work' } })
    await settle()
    await vi.advanceTimersByTimeAsync(20)
    const input = document.querySelector<HTMLInputElement>('#chat-search-input')!
    input.value = 'history'
    input.dispatchEvent(new Event('input', { bubbles: true }))
    await nextTick()
    await vi.advanceTimersByTimeAsync(250)
    await nextTick()
    expect(document.body.textContent).toContain('Some large transcripts were skipped')

    document.querySelector<HTMLButtonElement>('.chat-search-more')!.click()
    await nextTick()
    await nextTick()
    expect(document.querySelector('[role="status"]')?.textContent).toContain('Search more failed')
    document.querySelector<HTMLButtonElement>('.chat-search-retry')!.click()
    await nextTick()
    await nextTick()
    expect(post.mock.calls[2]).toEqual(['/api/chats/search', { workspace: 'work', q: 'history', cursor: 'cursor-2' }])
    expect([...document.querySelectorAll('.chat-search-result-title')].map(node => node.textContent)).toEqual(['First', 'Second'])
    expect(document.body.textContent).toContain('Some large transcripts were skipped')
    wrapper.unmount()
  })

  it('shows the project with a labelled Archived chip, or the chip alone without a project', async () => {
    vi.useFakeTimers()
    post.mockResolvedValue({ items: [
      { chat_id: 'a', title: 'Live', project_title: 'Upwordo', archived: false, snippet: '' },
      { chat_id: 'b', title: 'Old', project_title: 'Upwordo', archived: true, snippet: '' },
      { chat_id: 'c', title: 'Orphan', project_title: '', archived: true, snippet: '' },
    ], next_cursor: null, skipped_large_transcripts: false })
    const wrapper = mount(ChatSearchDialog, { attachTo: document.body, props: { open: true, workspace: 'work' } })
    await settle()
    await vi.advanceTimersByTimeAsync(20)
    await searchFor('upwordo')
    const metas = [...document.querySelectorAll('.chat-search-meta')]
    expect(metas).toHaveLength(3)
    expect(metas[0]!.querySelector('.chat-search-project')?.textContent).toBe('Upwordo')
    expect(metas[0]!.querySelector('.chat-search-chip')).toBeNull()
    expect(metas[1]!.querySelector('.chat-search-project')?.textContent).toBe('Upwordo')
    expect(metas[1]!.querySelector('.chat-search-chip')?.textContent).toBe('Archived')
    expect(metas[2]!.querySelector('.chat-search-project')).toBeNull()
    expect(metas[2]!.querySelector('.chat-search-chip')?.textContent).toBe('Archived')
    wrapper.unmount()
  })

  it('renders snippets as plain text and highlights query terms in title and snippet', async () => {
    vi.useFakeTimers()
    post.mockResolvedValue({ items: [
      { chat_id: 'a', title: 'Haven fix', project_title: 'P', archived: false, snippet: "**haven't** merged [PR #254](https://x.test/254)\n1. done <b>x</b>" },
    ], next_cursor: null, skipped_large_transcripts: false })
    const wrapper = mount(ChatSearchDialog, { attachTo: document.body, props: { open: true, workspace: 'work' } })
    await settle()
    await vi.advanceTimersByTimeAsync(20)
    await searchFor('haven pr')
    const snippet = document.querySelector('.chat-search-snippet')!
    expect(snippet.textContent).toBe("haven't merged PR #254 done <b>x</b>")
    expect(snippet.querySelector('b')).toBeNull()
    expect([...snippet.querySelectorAll('mark')].map(m => m.textContent)).toEqual(['haven', 'PR'])
    expect([...document.querySelectorAll('.chat-search-result-title mark')].map(m => m.textContent)).toEqual(['Haven'])
    wrapper.unmount()
  })

  it('moves a selection with Up and Down from the input and opens it with Enter', async () => {
    vi.useFakeTimers()
    post.mockResolvedValue({ items: [
      { chat_id: 'one', title: 'One', project_title: 'P', archived: false, snippet: '' },
      { chat_id: 'two', title: 'Two', project_title: 'P', archived: false, snippet: '' },
    ], next_cursor: null, skipped_large_transcripts: false })
    const wrapper = mount(ChatSearchDialog, { attachTo: document.body, props: { open: true, workspace: 'work' } })
    await settle()
    await vi.advanceTimersByTimeAsync(20)
    const input = await searchFor('match')
    const key = async (name: string) => { input.dispatchEvent(new KeyboardEvent('keydown', { key: name, bubbles: true, cancelable: true })); await nextTick() }
    const options = () => [...document.querySelectorAll('[role="option"]')]
    expect(input.getAttribute('role')).toBe('combobox')
    expect(input.hasAttribute('aria-activedescendant')).toBe(false)
    await key('ArrowDown')
    expect(options()[0]!.getAttribute('aria-selected')).toBe('true')
    expect(input.getAttribute('aria-activedescendant')).toBe(options()[0]!.id)
    await key('ArrowDown')
    await key('ArrowDown')
    expect(options().map(o => o.getAttribute('aria-selected'))).toEqual(['false', 'true'])
    await key('ArrowUp')
    await key('ArrowUp')
    expect(input.hasAttribute('aria-activedescendant')).toBe(false)
    await key('ArrowDown')
    await key('ArrowDown')
    expect(document.activeElement).toBe(input)
    await key('Enter')
    expect(wrapper.emitted('select')?.at(-1)).toEqual(['two'])
    wrapper.unmount()
  })

  it('names the field with a visually hidden label and a placeholder', async () => {
    const wrapper = mount(ChatSearchDialog, { attachTo: document.body, props: { open: true, workspace: 'work' } })
    await settle()
    const label = document.querySelector<HTMLLabelElement>('label[for="chat-search-input"]')!
    expect(label.className).toBe('sr-only')
    expect(label.textContent).toBe('Title or conversation text')
    expect(document.querySelector<HTMLInputElement>('#chat-search-input')!.placeholder).toBe('Title or conversation text')
    wrapper.unmount()
  })
})
