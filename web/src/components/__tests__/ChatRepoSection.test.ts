// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import ChatRepoSection from '../ChatRepoSection.vue'
import { useProjectStore } from '../../stores/projects'
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
    expect(wrapper.find('[role=switch]').exists()).toBe(true)
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

  it("shows the code-mode explanation as the row's detail and describes the switch with it", () => {
    const wrapper = mountSection(repo, {})
    expect(wrapper.find('.repo-code-info').exists()).toBe(false)
    expect(wrapper.find('[aria-label="About code mode"]').exists()).toBe(false)
    const hint = wrapper.get('.repo-code-hint')
    expect(hint.classes()).not.toContain('sr-only')
    expect(hint.text()).toBe("Loads the repository's instructions, skills and write access in this chat.")
    expect(wrapper.get('[role=switch]').attributes('aria-describedby')).toBe(hint.element.id)
    wrapper.unmount()
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

  it('names a locked code mode without changing switch semantics', () => {
    const wrapper = mountSection(repo, {}, { codeMode: true, codeModeLocked: true })
    const note = wrapper.get('.repo-code-lock-note')
    expect(note.text()).toBe('Locked on for this chat')
    // The note sits under the label, not inside the switch: the switch still reads On.
    const sw = wrapper.get('[role=switch]')
    expect(sw.text()).toBe('On')
    expect(sw.find('.repo-code-lock-note').exists()).toBe(false)
    expect(sw.attributes('aria-checked')).toBe('true')
    expect(sw.attributes('aria-disabled')).toBe('true')
    expect(wrapper.get('.repo-code-hint').classes()).toContain('sr-only')
    wrapper.unmount()
  })

  it('says nothing about a lock while code mode can still change', () => {
    const wrapper = mountSection(repo, {}, { codeMode: true })
    expect(wrapper.find('.repo-code-lock-note').exists()).toBe(false)
    expect(wrapper.get('[role=switch]').attributes('aria-disabled')).toBe('false')
    wrapper.unmount()
  })
})
