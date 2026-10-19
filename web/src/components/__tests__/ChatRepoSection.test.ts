// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import ChatRepoSection from '../ChatRepoSection.vue'
import { useProjectStore } from '../../stores/projects'
import { api } from '../../lib/api'
import { resetHostTerminalForTests } from '../../composables/useHostTerminal'
import type { ProjectRepo, RepoLocation } from '../../lib/types'

const repo: ProjectRepo = { path: '/r/app', branch: 'main', dirty: false, missing: false }
const worktree: RepoLocation = { path: '/r/app-wt', branch: 'feat-x', is_worktree: true, exists: true }

function mountSection(
  r: ProjectRepo,
  location: Partial<RepoLocation> | null,
  opts: { codeMode?: boolean; archived?: boolean } = {},
) {
  return mount(ChatRepoSection, {
    attachTo: document.body,
    props: { repo: r, location, chatId: 'c1', codeMode: opts.codeMode ?? false, archived: opts.archived ?? false },
  })
}

describe('ChatRepoSection', () => {
  beforeEach(() => { setActivePinia(createPinia()) })
  afterEach(() => { document.body.innerHTML = ''; vi.unstubAllGlobals() })

  it('shows name, branch and clean state for the main checkout', () => {
    const wrapper = mountSection(repo, {})
    expect(wrapper.get('.chat-repo-name').text()).toBe('app')
    expect(wrapper.get('.chat-repo-name').attributes('title')).toBe('/r/app')
    expect(wrapper.text()).not.toContain('/r/app')
    expect(wrapper.get('.chat-repo-branch').text()).toContain('main')
    expect(wrapper.get('.chat-repo-clean').text()).toBe('clean')
    expect(wrapper.find('.chat-repo-branch').attributes('title')).toBeUndefined()
    wrapper.unmount()
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
    resetHostTerminalForTests()
    vi.spyOn(api, 'get').mockResolvedValue({
      available: true, platform: 'macos', reason: '', terminal: 'Terminal', actions: [],
    })
    const wrapper = mountSection(repo, {})
    await flushPromises()
    const terminal = wrapper.get('.chat-repo-terminal').element
    const row = wrapper.get('.repo-code-row').element
    expect(terminal.compareDocumentPosition(row) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    wrapper.unmount()
    resetHostTerminalForTests()
  })

  it('offers Open in Terminal at the chat folder when the engine can open one', async () => {
    resetHostTerminalForTests()
    vi.spyOn(api, 'get').mockResolvedValue({
      available: true, platform: 'macos', reason: '', terminal: 'Terminal', actions: [],
    })
    const post = vi.spyOn(api, 'post').mockResolvedValue({ ok: true, platform: 'macos', launched: 'Terminal' })
    const wrapper = mountSection(repo, {})
    await flushPromises()
    const button = wrapper.findAll('button').find((b) => b.text() === 'Open in Terminal')
    expect(button).toBeDefined()
    await button!.trigger('click')
    await flushPromises()
    expect(post).toHaveBeenCalledWith('/api/host/terminal', { open: 'folder', chat_id: 'c1' })
    wrapper.unmount()
    resetHostTerminalForTests()
  })

  it('marks the terminal link with chat-repo-terminal when the engine can open one', async () => {
    resetHostTerminalForTests()
    vi.spyOn(api, 'get').mockResolvedValue({
      available: true, platform: 'macos', reason: '', terminal: 'Terminal', actions: [],
    })
    const wrapper = mountSection(repo, {})
    await flushPromises()
    expect(wrapper.find('.chat-repo-terminal').exists()).toBe(true)
    wrapper.unmount()
    resetHostTerminalForTests()
  })
})
