// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import ChatRepoSection from '../ChatRepoSection.vue'
import type { ProjectRepo, RepoLocation } from '../../lib/types'

const repo: ProjectRepo = { path: '/r/app', branch: 'main', dirty: false, missing: false }
const worktree: RepoLocation = { path: '/r/app-wt', branch: 'feat-x', is_worktree: true, exists: true }

function mountSection(r: ProjectRepo, location: Partial<RepoLocation> | null) {
  return mount(ChatRepoSection, {
    attachTo: document.body,
    props: { repo: r, location, chatId: 'c1' },
  })
}

describe('ChatRepoSection', () => {
  beforeEach(() => { setActivePinia(createPinia()) })
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
    // A missing folder takes the chat out of code mode, so the note is not shown.
    expect(wrapper.find('.chat-repo-code-note').exists()).toBe(false)
    wrapper.unmount()
  })

  it('renders no code-mode switch: code mode follows the repository link', () => {
    const wrapper = mountSection(repo, worktree)
    expect(wrapper.find('[role=switch]').exists()).toBe(false)
    expect(wrapper.find('input[type=checkbox]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('Code mode')
    expect(wrapper.text()).not.toContain('Locked on')
    expect(wrapper.get('.chat-repo-code-note').text()).toContain('work in code mode')
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
