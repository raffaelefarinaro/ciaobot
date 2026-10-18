// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import ChatRepoChip from '../ChatRepoChip.vue'
import { useProjectStore } from '../../stores/projects'
import type { ProjectRepo, RepoLocation } from '../../lib/types'

const repo: ProjectRepo = { path: '/r/app', branch: 'main', dirty: false, missing: false }
const worktree: RepoLocation = { path: '/r/app-wt', branch: 'feat-x', is_worktree: true, exists: true }

function mountChip(r: ProjectRepo | null, location: Partial<RepoLocation> | null, codeMode = false) {
  return mount(ChatRepoChip, { attachTo: document.body, props: { repo: r, location, chatId: 'c1', codeMode } })
}

describe('ChatRepoChip', () => {
  beforeEach(() => { setActivePinia(createPinia()) })
  afterEach(() => { document.body.innerHTML = '' })

  it('renders nothing without a linked repo', () => {
    const wrapper = mountChip(null, worktree)
    expect(wrapper.find('.repo-chip').exists()).toBe(false)
    wrapper.unmount()
  })

  it('shows name, branch, worktree and the accessible label', () => {
    const wrapper = mountChip(repo, worktree)
    const btn = wrapper.get('.repo-chip')
    expect(btn.text()).toContain('app')
    expect(btn.text()).toContain('feat-x')
    expect(btn.text()).toContain('(worktree)')
    expect(btn.attributes('aria-label')).toBe('Repository app, branch feat-x, worktree')
    wrapper.unmount()
  })

  it('opens a popover with folder, branch and checkout on click', async () => {
    const wrapper = mountChip(repo, worktree)
    await wrapper.get('.repo-chip').trigger('click')
    await nextTick()
    const pop = document.querySelector('.repo-popover')
    expect(pop).not.toBeNull()
    // Rendered by this component's own template, so its scoped card styles apply.
    expect(Array.from(pop!.attributes).some((a) => a.name.startsWith('data-v-'))).toBe(true)
    expect(pop!.textContent).toContain('/r/app-wt')
    expect(pop!.textContent).toContain('feat-x')
    expect(pop!.textContent).toContain('Worktree')
    wrapper.unmount()
  })

  it('shows the dirty state only for the main checkout', async () => {
    const main = mountChip({ ...repo, dirty: true }, {})
    expect(main.find('.repo-chip-dot').exists()).toBe(true)
    await main.get('.repo-chip').trigger('click')
    await nextTick()
    expect(document.querySelector('.repo-popover')!.textContent).toContain('Uncommitted changes')
    main.unmount()
    document.body.innerHTML = ''

    const wt = mountChip({ ...repo, dirty: true }, worktree)
    expect(wt.find('.repo-chip-dot').exists()).toBe(false)
    await wt.get('.repo-chip').trigger('click')
    await nextTick()
    expect(document.querySelector('.repo-popover')!.textContent).not.toContain('Changes')
    wt.unmount()
  })

  it('shows the on state with a leading code label', () => {
    const wrapper = mountChip(repo, worktree, true)
    const btn = wrapper.get('.repo-chip')
    expect(wrapper.find('.repo-chip--code').exists()).toBe(true)
    expect(btn.text().startsWith('code')).toBe(true)
    expect(btn.attributes('aria-label')?.endsWith(', code mode on')).toBe(true)
    wrapper.unmount()
  })

  it('toggles code mode from the popover switch', async () => {
    const store = useProjectStore()
    const spy = vi.spyOn(store, 'setChatCodeMode').mockResolvedValue()
    const wrapper = mountChip(repo, worktree)
    await wrapper.get('.repo-chip').trigger('click')
    await nextTick()
    const sw = document.querySelector('[role=switch]') as HTMLButtonElement
    expect(sw.tagName).toBe('BUTTON')
    expect(sw.getAttribute('type')).toBe('button')
    expect(sw.getAttribute('aria-checked')).toBe('false')
    sw.click()
    await flushPromises()
    expect(spy).toHaveBeenCalledWith('c1', true)
    wrapper.unmount()
  })

  it("shows the engine's error inline", async () => {
    const store = useProjectStore()
    vi.spyOn(store, 'setChatCodeMode').mockRejectedValue({ payload: { error: 'Link a repository first' } })
    const wrapper = mountChip(repo, worktree)
    await wrapper.get('.repo-chip').trigger('click')
    await nextTick()
    const sw = document.querySelector('[role=switch]') as HTMLButtonElement
    sw.click()
    await flushPromises()
    expect(document.querySelector('[role=alert]')!.textContent).toContain('Link a repository first')
    expect(sw.disabled).toBe(false)
    wrapper.unmount()
  })
})
