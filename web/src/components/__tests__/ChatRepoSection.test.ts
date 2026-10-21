// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import ChatRepoSection from '../ChatRepoSection.vue'
import { useProjectStore } from '../../stores/projects'
import { useTerminalStore } from '../../stores/terminal'
import { api } from '../../lib/api'
import type { ProjectRepo, RepoLocation } from '../../lib/types'

const repo: ProjectRepo = { path: '/r/app', branch: 'main', dirty: false, missing: false }
const worktree: RepoLocation = { path: '/r/app-wt', branch: 'feat-x', is_worktree: true, exists: true }

function mountSection(
  r: ProjectRepo,
  location: Partial<RepoLocation> | null,
  opts: { codeMode?: boolean; codeModeLocked?: boolean; archived?: boolean } = {},
) {
  return mount(ChatRepoSection, {
    attachTo: document.body,
    props: {
      repo: r,
      location,
      chatId: 'c1',
      codeMode: opts.codeMode ?? false,
      codeModeLocked: opts.codeModeLocked ?? false,
      archived: opts.archived ?? false,
    },
  })
}

describe('ChatRepoSection', () => {
  beforeEach(() => { setActivePinia(createPinia()) })
  afterEach(() => { document.body.innerHTML = ''; vi.unstubAllGlobals() })

  it('shows name, branch and clean state for the main checkout', () => {
    const wrapper = mountSection(repo, {})
    expect(wrapper.get('.chat-repo-name').text()).toBe('app')
    expect(wrapper.get('.chat-repo-name').attributes('title')).toBe('/r/app')
    // The summary carries the branch, not the folder; the folder is in the body.
    expect(wrapper.get('.chat-repo-branch').text()).not.toContain('/r/app')
    expect(wrapper.get('.chat-repo-branch').text()).toContain('main')
    expect(wrapper.get('.chat-repo-clean').text()).toBe('clean')
    expect(wrapper.find('.chat-repo-branch').attributes('title')).toBeUndefined()
    wrapper.unmount()
  })

  it('keeps a full branch behind a closed disclosure', () => {
    const wrapper = mountSection(repo, {})
    const details = wrapper.get('details.chat-repo-branch-details')
    expect(details.attributes('open')).toBeUndefined()
    expect(details.get('summary.chat-repo-branch').text()).toContain('main')
    // The whole branch and the folder the chat works in are one click away.
    expect(details.get('.chat-repo-branch-value').text()).toBe('main')
    expect(details.get('.chat-repo-path-value').text()).toBe('/r/app')
    wrapper.unmount()
  })

  it('keeps the whole branch text in the body when the summary has to cut it', () => {
    const branch = 'feature/a-very-long-branch-name-for-the-rail'
    const wrapper = mountSection({ ...repo, branch }, {})
    const body = wrapper.get('.chat-repo-branch-body')
    expect(body.get('.chat-repo-branch-value').text()).toBe(branch)
    expect(body.get('.chat-repo-path-value').text()).toBe('/r/app')
    wrapper.unmount()
  })

  it('shows the actual location when the chat works away from the checkout', () => {
    const wrapper = mountSection(repo, { path: '/r/app-wt', branch: 'feat-x', is_worktree: true, exists: true })
    const body = wrapper.get('.chat-repo-branch-body')
    expect(body.get('.chat-repo-branch-value').text()).toBe('feat-x')
    expect(body.get('.chat-repo-path-value').text()).toBe('/r/app-wt')
    wrapper.unmount()
  })

  it('puts the whole branch and path inside the disclosure, and the facts outside it', () => {
    const wrapper = mountSection(repo, { path: '/r/app-wt', branch: 'feat-x', is_worktree: true, exists: true })
    const details = wrapper.get('details.chat-repo-branch-details')
    const summary = details.get('summary.chat-repo-branch')
    // The interactive disclosure is the summary; the body is its content, and
    // the checkout facts stay beside it on the same line rather than inside it.
    expect(summary.element.tagName).toBe('SUMMARY')
    expect(details.element.firstElementChild).toBe(summary.element)
    expect(details.get('.chat-repo-branch-body').text()).toContain('feat-x')
    expect(details.find('.chat-repo-kind').exists()).toBe(false)
    const line = wrapper.get('.chat-repo-branch-line')
    expect(line.get('.chat-repo-kind').text()).toBe('worktree')
    // Exactly one focusable control in the branch row: the disclosure itself.
    const focusables = line.findAll('summary, button, a, input, select, textarea, [tabindex]')
    expect(focusables).toHaveLength(1)
    expect(focusables[0].element).toBe(summary.element)
    wrapper.unmount()
  })

  it('keeps worktree and dirty facts visible', () => {
    // A worktree reports no dirtiness of its own, so it carries the tag only.
    const inWorktree = mountSection(repo, worktree)
    expect(inWorktree.get('.chat-repo-kind').text()).toBe('worktree')
    // The facts sit outside the closed branch disclosure.
    expect(inWorktree.get('.chat-repo-kind').element.closest('details')).toBeNull()
    inWorktree.unmount()

    const dirty = mountSection({ ...repo, dirty: true }, {})
    expect(dirty.get('.chat-repo-dirty').text()).toContain('uncommitted changes')
    expect(dirty.get('.chat-repo-dirty').element.closest('details')).toBeNull()
    dirty.unmount()
  })

  it("shows the chat's worktree and no dirty state", () => {
    const wrapper = mountSection({ ...repo, dirty: true }, worktree)
    expect(wrapper.get('.chat-repo-branch').text()).toContain('feat-x')
    expect(wrapper.get('.chat-repo-kind').text()).toBe('worktree')
    expect(wrapper.text()).not.toContain('This chat works in')
    expect(wrapper.get('.chat-repo-branch').attributes('title')).toBe('Main checkout is on main')
    expect(wrapper.find('.chat-repo-dirty').exists()).toBe(false)
    expect(wrapper.find('.chat-repo-clean').exists()).toBe(false)
    wrapper.unmount()
  })

  it('shows uncommitted changes as a dot plus words', () => {
    const wrapper = mountSection({ ...repo, dirty: true }, {})
    const dirty = wrapper.get('.chat-repo-dirty')
    expect(dirty.text()).toContain('uncommitted changes')
    expect(dirty.find('.chat-repo-dot').attributes('aria-hidden')).toBe('true')
    wrapper.unmount()
  })

  it('warns when the folder is missing and hides the copy button', () => {
    const wrapper = mountSection({ ...repo, missing: true }, {})
    expect(wrapper.get('.chat-repo-missing').text()).toContain('folder is missing')
    expect(wrapper.find('.chat-repo-meta').exists()).toBe(false)
    expect(wrapper.find('.chat-repo-copy').exists()).toBe(false)
    wrapper.unmount()
  })

  it('toggles code mode from the switch', async () => {
    const store = useProjectStore()
    const spy = vi.spyOn(store, 'setChatCodeMode').mockResolvedValue()
    const wrapper = mountSection(repo, worktree)
    const sw = wrapper.get('button[type=button][role=switch]')
    expect(sw.attributes('aria-checked')).toBe('false')
    await sw.trigger('click')
    await flushPromises()
    expect(spy).toHaveBeenCalledWith('c1', true)
    wrapper.unmount()
  })

  it("shows the engine's error inline", async () => {
    const store = useProjectStore()
    vi.spyOn(store, 'setChatCodeMode').mockRejectedValue({ payload: { error: 'Link a repository first' } })
    const wrapper = mountSection(repo, worktree)
    const sw = wrapper.get('[role=switch]')
    await sw.trigger('click')
    await flushPromises()
    expect(wrapper.get('[role=alert]').text()).toContain('Link a repository first')
    expect(sw.attributes('aria-disabled')).toBe('false')
    wrapper.unmount()
  })

  it('hides the switch on an archived chat', () => {
    const wrapper = mountSection(repo, {}, { archived: true })
    expect(wrapper.find('[role=switch]').exists()).toBe(false)
    expect(wrapper.find('.repo-code-hint').exists()).toBe(false)
    wrapper.unmount()
  })

  it("copies the chat's folder path", async () => {
    vi.useFakeTimers()
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('navigator', { clipboard: { writeText } })
    const wrapper = mountSection(repo, worktree)
    const copy = wrapper.get('.chat-repo-copy')
    expect(copy.attributes('aria-label')).toBe('Copy path')
    await copy.trigger('click')
    await vi.advanceTimersByTimeAsync(0)
    expect(writeText).toHaveBeenCalledWith('/r/app-wt')
    expect(copy.attributes('aria-label')).toBe('Path copied')
    await vi.advanceTimersByTimeAsync(1500)
    expect(copy.attributes('aria-label')).toBe('Copy path')
    wrapper.unmount()
    vi.useRealTimers()
  })

  it('keeps the code-mode hint behind the info button', async () => {
    const wrapper = mountSection(repo, {})
    const info = wrapper.get('.repo-code-info')
    const hint = wrapper.get('.repo-code-hint').element.parentElement!
    expect(info.attributes('aria-expanded')).toBe('false')
    expect(hint.classList.contains('sr-only')).toBe(true)
    expect(info.attributes('aria-controls')).toBe(hint.id)
    const describedBy = wrapper.get('[role=switch]').attributes('aria-describedby')!
    expect(wrapper.get('.repo-code-hint').element.id).toBe(describedBy)
    await info.trigger('click')
    expect(info.attributes('aria-expanded')).toBe('true')
    expect(hint.classList.contains('sr-only')).toBe(false)
    expect(document.getElementById(describedBy)).not.toBeNull()
    wrapper.unmount()
  })

  it('puts Open in Terminal before the code-mode row', async () => {
    const wrapper = mountSection(repo, {})
    await flushPromises()
    const terminal = wrapper.get('.chat-repo-terminal').element
    const row = wrapper.get('.repo-code-row').element
    expect(terminal.compareDocumentPosition(row) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    wrapper.unmount()
  })

  it('opens the browser terminal for the chat', async () => {
    // The chat's terminal lives in this app now: the button opens the pane
    // under the chat, and it asks the engine about that one chat.
    vi.spyOn(api, 'get').mockImplementation((path: string) => Promise.resolve(
      path === '/api/terminal/status'
        ? { available: true, reason: '', platform: 'macos', host: 'engine.local', unlock_expires_in: 900, detached_ttl_seconds: 600 }
        : { sessions: [] },
    ))
    const store = useTerminalStore()
    const post = vi.spyOn(api, 'post').mockResolvedValue({ ok: true })
    const wrapper = mountSection(repo, {})
    await flushPromises()
    const button = wrapper.findAll('button').find((b) => b.text() === 'Open in Terminal')
    expect(button).toBeDefined()
    await button!.trigger('click')
    await flushPromises()
    expect(store.visible).toBe(true)
    expect(store.target).toEqual({ chatId: 'c1' })
    // The native host terminal is a different route, and this button does not
    // open one.
    expect(post).not.toHaveBeenCalledWith('/api/host/terminal', expect.anything())
    wrapper.unmount()
    useTerminalStore().dispose()
  })

  it('marks the terminal link with chat-repo-terminal', async () => {
    const wrapper = mountSection(repo, {})
    await flushPromises()
    expect(wrapper.find('.chat-repo-terminal').exists()).toBe(true)
    wrapper.unmount()
  })

  it('names a locked code mode without changing switch semantics', () => {
    const wrapper = mountSection(repo, {}, { codeMode: true, codeModeLocked: true })
    const note = wrapper.get('.repo-code-lock-note')
    expect(note.text()).toBe('locked for this chat')
    // The note sits beside the switch, not inside it: the switch still reads On.
    const sw = wrapper.get('[role=switch]')
    expect(sw.text()).toBe('On')
    expect(sw.find('.repo-code-lock-note').exists()).toBe(false)
    expect(sw.attributes('aria-checked')).toBe('true')
    expect(sw.attributes('aria-disabled')).toBe('true')
    wrapper.unmount()
  })

  it('says nothing about a lock while code mode can still change', () => {
    const wrapper = mountSection(repo, {}, { codeMode: true })
    expect(wrapper.find('.repo-code-lock-note').exists()).toBe(false)
    expect(wrapper.get('[role=switch]').attributes('aria-disabled')).toBe('false')
    wrapper.unmount()
  })
})
