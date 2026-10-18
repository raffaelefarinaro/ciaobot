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

  it('shows name, path, branch and clean state for the main checkout', () => {
    const wrapper = mountSection(repo, {})
    expect(wrapper.get('.chat-repo-name').text()).toBe('app')
    expect(wrapper.get('.chat-repo-path').text()).toBe('/r/app')
    expect(wrapper.get('.chat-repo-branch').text()).toContain('main')
    expect(wrapper.get('.chat-repo-clean').text()).toBe('clean')
    expect(wrapper.find('.chat-repo-note').exists()).toBe(false)
    wrapper.unmount()
  })

  it("shows the chat's worktree, its note, and no dirty state", () => {
    const wrapper = mountSection({ ...repo, dirty: true }, worktree)
    expect(wrapper.get('.chat-repo-branch').text()).toContain('feat-x')
    expect(wrapper.get('.chat-repo-kind').text()).toBe('worktree')
    expect(wrapper.get('.chat-repo-note').text()).toContain('/r/app-wt')
    expect(wrapper.get('.chat-repo-note').text()).toContain('main')
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

  it('warns when the folder is missing and hides Open AGENTS.md', () => {
    const wrapper = mountSection({ ...repo, missing: true }, {})
    expect(wrapper.get('.chat-repo-missing').text()).toContain('folder is missing')
    expect(wrapper.find('.chat-repo-meta').exists()).toBe(false)
    expect(wrapper.find('.chat-repo-open').exists()).toBe(false)
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

  it('opens AGENTS.md through findRepoInstructions', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, status: 200 })
    vi.stubGlobal('fetch', fetchMock)
    const wrapper = mountSection(repo, {})
    await wrapper.get('.chat-repo-open').trigger('click')
    await flushPromises()
    expect(fetchMock.mock.calls[0][0]).toContain('exact=1')
    expect(wrapper.emitted('open-file')).toEqual([['/r/app/AGENTS.md']])
    expect(wrapper.find('[role=alert]').exists()).toBe(false)

    fetchMock.mockResolvedValue({ ok: false, status: 404 })
    await wrapper.get('.chat-repo-open').trigger('click')
    await flushPromises()
    expect(wrapper.emitted('open-file')).toHaveLength(1)
    expect(wrapper.get('[role=alert]').text()).toContain('no AGENTS.md or CLAUDE.md')
    wrapper.unmount()
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
})
