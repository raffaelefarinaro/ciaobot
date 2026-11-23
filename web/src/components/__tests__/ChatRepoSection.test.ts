// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
vi.mock('../../lib/api', () => ({ api: { get: vi.fn() } }))
import ChatRepoSection from '../ChatRepoSection.vue'
import { api } from '../../lib/api'
import type { ProjectRepo, RepoLocation } from '../../lib/types'

const repo: ProjectRepo = { path: '/r/app', branch: 'main', dirty: false, missing: false }
const worktree: RepoLocation = { path: '/r/app-wt', branch: 'feat-x', is_worktree: true, exists: true }

function mountSection(r: ProjectRepo, location: Partial<RepoLocation> | null, eligible = false) {
  return mount(ChatRepoSection, {
    attachTo: document.body,
    props: { repo: r, location, chatId: 'c1', eligible },
  })
}

describe('ChatRepoSection', () => {
  beforeEach(() => { setActivePinia(createPinia()); vi.mocked(api.get).mockReset() })
  afterEach(() => { document.body.innerHTML = ''; vi.unstubAllGlobals() })

  it('shows name, branch and clean state for the main checkout', () => {
    const wrapper = mountSection(repo, {})
    expect(wrapper.get('.cd-heading').text()).toBe('Repository')
    expect(wrapper.get('section').attributes('aria-labelledby')).toBe(wrapper.get('.cd-heading').attributes('id'))
    expect(wrapper.get('.chat-repo-name').text()).toBe('app')
    expect(wrapper.get('.chat-repo-path').text()).toBe('/r/app')
    expect(wrapper.get('.chat-repo-path').attributes('title')).toBe('/r/app')
    // The summary carries the branch, not the folder; the folder is in the body.
    expect(wrapper.get('.chat-repo-branch').text()).not.toContain('/r/app')
    expect(wrapper.get('.chat-repo-branch').text()).toContain('main')
    expect(wrapper.get('.chat-repo-clean').text()).toBe('clean')
    expect(wrapper.find('.chat-repo-branch').attributes('title')).toBeUndefined()
    wrapper.unmount()
  })

  it('fetches only after opening and renders patch text and untracked names literally', async () => {
    const response = { state: 'ready' as const, source: 'worktree' as const, staged: '<script>staged</script>', unstaged: '', untracked: ['new\nfile.txt'], truncated: false }
    vi.mocked(api.get).mockResolvedValue(response)
    const wrapper = mountSection(repo, worktree, true)
    expect(api.get).not.toHaveBeenCalled()
    await wrapper.get('.chat-repo-changes-button').trigger('click')
    await flushPromises()
    expect(api.get).toHaveBeenCalledWith('/api/chats/c1/worktree-changes')
    expect(wrapper.get('.chat-repo-changes').text()).toContain('Worktree changes')
    expect(wrapper.get('.chat-repo-changes pre').text()).toBe('<script>staged</script>')
    expect(wrapper.find('script').exists()).toBe(false)
    expect(wrapper.text()).toContain('new\nfile.txt')
    wrapper.unmount()
  })

  it('discards an old response after the chat changes', async () => {
    let resolve!: (value: unknown) => void
    vi.mocked(api.get).mockReturnValue(new Promise((done) => { resolve = done }) as ReturnType<typeof api.get>)
    const wrapper = mountSection(repo, worktree, true)
    await wrapper.get('.chat-repo-changes-button').trigger('click')
    await wrapper.setProps({ chatId: 'c2' })
    resolve({ state: 'ready', source: 'worktree', staged: 'stale', unstaged: '', untracked: [], truncated: false })
    await flushPromises()
    expect(wrapper.text()).not.toContain('stale')
    expect(wrapper.find('.chat-repo-changes').exists()).toBe(false)
    wrapper.unmount()
  })

  it('shows linked checkout source, clean and truncation states, with retry after a read error', async () => {
    vi.mocked(api.get).mockRejectedValueOnce(new Error('private host detail'))
      .mockResolvedValueOnce({ state: 'ready', source: 'linked_checkout', staged: '', unstaged: '', untracked: [], truncated: true })
    const wrapper = mountSection(repo, {}, true)
    await wrapper.get('.chat-repo-changes-button').trigger('click')
    await flushPromises()
    expect(wrapper.get('[role=alert]').text()).toContain('Could not read checkout changes')
    expect(wrapper.text()).not.toContain('private host detail')
    await wrapper.get('.chat-repo-changes button').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('Linked checkout changes')
    expect(wrapper.text()).toContain('No uncommitted changes.')
    expect(wrapper.text()).toContain('Snapshot truncated')
    wrapper.unmount()
  })

  it('has no branch disclosure: the branch row holds no control', () => {
    const wrapper = mountSection(repo, {})
    expect(wrapper.find('button.chat-repo-branch').exists()).toBe(false)
    expect(wrapper.find('.chat-repo-branch-body').exists()).toBe(false)
    expect(wrapper.get('.chat-repo-branch-row').findAll('button, a, summary, [tabindex]')).toHaveLength(0)
    expect(wrapper.get('.chat-repo-branch').text()).toContain('main')
    wrapper.unmount()
  })

  it("keeps the whole branch in the row's text", () => {
    const branch = 'feature/a-very-long-branch-name-for-the-rail'
    const wrapper = mountSection({ ...repo, branch }, {})
    expect(wrapper.get('.chat-repo-branch').text()).toContain(branch)
    expect(wrapper.get('.chat-repo-path').text()).toBe('/r/app')
    wrapper.unmount()
  })

  it('shows the actual location when the chat works away from the checkout', () => {
    const wrapper = mountSection(repo, { path: '/r/app-wt', branch: 'feat-x', is_worktree: true, exists: true })
    expect(wrapper.get('.chat-repo-branch').text()).toContain('feat-x')
    expect(wrapper.get('.chat-repo-path').text()).toBe('/r/app-wt')
    wrapper.unmount()
  })

  it("puts the worktree fact in the branch row's detail line", () => {
    const wrapper = mountSection(repo, { path: '/r/app-wt', branch: 'feat-x', is_worktree: true, exists: true })
    expect(wrapper.get('.chat-repo-branch-row .chat-repo-facts .chat-repo-kind').text()).toBe('worktree')
    expect(wrapper.get('.chat-repo-branch-row').findAll('summary, button, a, input, select, textarea, [tabindex]')).toHaveLength(0)
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
    expect(wrapper.find('.chat-repo-branch-row').exists()).toBe(false)
    expect(wrapper.findAll('.chat-repo-row')).toHaveLength(1)
    expect(wrapper.find('.chat-repo-copy').exists()).toBe(false)
    wrapper.unmount()
  })

  it('renders no code-mode switch: code mode follows the repository link', () => {
    const wrapper = mountSection(repo, worktree)
    expect(wrapper.find('[role=switch]').exists()).toBe(false)
    expect(wrapper.find('input[type=checkbox]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('Code mode')
    expect(wrapper.text()).not.toContain('Locked on')
    expect(wrapper.text()).not.toContain('work in code mode')
    // The only control left is the path copy button.
    const buttons = wrapper.findAll('button')
    expect(buttons).toHaveLength(1)
    expect(buttons[0].classes()).toContain('chat-repo-copy')
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

  it('carries no terminal button: the terminal has its own section', async () => {
    // The terminal is not a repository fact — a chat with no repository still
    // needs one — so it lives in the chat's own Terminal section, and this
    // section carries the checkout facts alone.
    const wrapper = mountSection(repo, {})
    await flushPromises()
    const terminal = wrapper.findAll('button').filter(b => b.text() === 'Open in Terminal')
    expect(terminal).toHaveLength(0)
    expect(wrapper.find('.chat-repo-terminal').exists()).toBe(false)
    wrapper.unmount()
  })

  it('carries no terminal button when the folder is missing either', async () => {
    const wrapper = mountSection({ ...repo, missing: true }, {})
    await flushPromises()
    const terminal = wrapper.findAll('button').filter(b => b.text() === 'Open in Terminal')
    expect(terminal).toHaveLength(0)
    wrapper.unmount()
  })
})
