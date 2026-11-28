// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory, createRouter } from 'vue-router'
import { defineComponent } from 'vue'
import MemoryChangesPanel from '../MemoryChangesPanel.vue'
import MemoryChangesRail from '../MemoryChangesRail.vue'
import { api } from '../../lib/api'
import type { ChangesFeed } from '../../lib/changesFeed'
import { CHANGES_WORKSPACE, changesFeed, emptyChangesFeed } from '../../lib/changesFeedFixture'
import { useProjectStore } from '../../stores/projects'

const startFileDiscussion = vi.hoisted(() => vi.fn(async () => ({ chat_id: 'undo-chat' })))
vi.mock('../../lib/fileDiscussion', () => ({ startFileDiscussion }))
const fetchWorkspaceGuide = vi.hoisted(() => vi.fn(async () => ({ path: 'storybook-alpha/AGENTS.md', content: '', error: '' })))
vi.mock('../../lib/workspaceGuide', () => ({ fetchWorkspaceGuide }))

enableAutoUnmount(afterEach)

function serve(feed: ChangesFeed | Error) {
  return vi.spyOn(api, 'get').mockImplementation(async (path: string) => {
    if (path === `/api/memory/changes?workspace=${CHANGES_WORKSPACE}`) {
      if (feed instanceof Error) throw feed
      return feed as never
    }
    throw new Error(`unexpected GET ${path}`)
  })
}

async function mountPanel(component: typeof MemoryChangesPanel | typeof MemoryChangesRail = MemoryChangesPanel) {
  const Empty = defineComponent({ template: '<div />' })
  const router = createRouter({ history: createMemoryHistory(), routes: [{ path: '/:p(.*)*', component: Empty }] })
  await router.push('/memory/changes')
  const wrapper = mount(component, { global: { plugins: [router] }, attachTo: document.body })
  await flushPromises()
  return Object.assign(wrapper, { router })
}

describe('MemoryChangesPanel', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    useProjectStore().activeWorkspace = CHANGES_WORKSPACE
    startFileDiscussion.mockClear()
    fetchWorkspaceGuide.mockClear()
  })

  afterEach(() => {
    document.body.innerHTML = ''
    vi.restoreAllMocks()
  })

  it('lists the care run and the days, newest first, each entry a sentence and a source', async () => {
    serve(changesFeed)
    const wrapper = await mountPanel()
    const groups = wrapper.findAll('.mch-group')
    expect(groups.map(g => g.attributes('data-change-group'))).toEqual(['care', 'day'])
    expect(groups[0].get('.mch-group-title').text()).toMatch(/· Workspace care, \d\d:\d\d$/)
    expect(groups[0].get('.mch-group-meta').text()).toBe('3 steps · 5 changes')
    expect(groups[1].get('.mch-group-title').text()).toMatch(/· 2 archived chats$/)
    const first = groups[0].findAll('.mch-row')[0]
    expect(first.get('.mch-sentence').text()).toBe('Replaced or removed “Standup moved to 09:30 for the autumn” in always-loaded memory')
    expect(first.get('.mch-source').text()).toBe('Workspace care · Memory regions · expired')
    // The mark says its kind in words as well as shape.
    expect(first.get('.mch-mark .sr-only').text()).toBe('Memory:')
    // Undo only where the undo log holds the original; a suggestion offers Review instead.
    const actions = wrapper.findAll('.mch-row').map(row => row.findAll('button').map(b => b.text()))
    expect(actions).toEqual([['Undo'], ['Undo'], [], [], ['Review'], ['Undo'], [], []])
  })

  it('filters by kind with a pressed state and says so when a kind has nothing', async () => {
    serve({ ...changesFeed, groups: changesFeed.groups.map(g => ({ ...g, entries: g.entries.filter(e => e.area !== 'tasks') })) })
    const wrapper = await mountPanel()
    const tasks = wrapper.findAll('.mch-filter').find(b => b.text() === 'Tasks')!
    expect(tasks.attributes('aria-pressed')).toBe('false')
    await tasks.trigger('click')
    expect(tasks.attributes('aria-pressed')).toBe('true')
    expect(wrapper.findAll('.mch-row')).toHaveLength(0)
    expect(wrapper.get('.mch-empty').text()).toBe('Nothing of this kind recorded.')
    await wrapper.findAll('.mch-filter').find(b => b.text() === 'Skills')!.trigger('click')
    expect(wrapper.findAll('.mch-row').map(r => r.attributes('data-change-kind'))).toEqual(['skill-suggestion'])
  })

  it('undoes a note line in a chat with the note pinned and the earlier text quoted', async () => {
    serve(changesFeed)
    const wrapper = await mountPanel()
    const row = wrapper.findAll('.mch-row').find(r => r.text().includes('Launch planned for May'))!
    const button = row.get('button')
    expect(button.attributes('aria-label')).toBe('Undo in a chat: Replaced or removed “Launch planned for May” in Projects/Sample-Project.md')
    await button.trigger('click')
    await flushPromises()
    expect(startFileDiscussion).toHaveBeenCalledTimes(1)
    const [, options] = startFileDiscussion.mock.calls[0] as unknown as [unknown, { path: string; seed: string; title: string; workspace: string }]
    expect(options.path).toBe(`memory-vault/${CHANGES_WORKSPACE}/Projects/Sample-Project.md`)
    expect(options.workspace).toBe(CHANGES_WORKSPACE)
    expect(options.title).toBe('Undo: Sample-Project.md')
    expect(options.seed).toContain('Earlier text: Launch planned for May')
    expect(options.seed).toContain('Memory pass for “Sample booking cancellation”')
    expect(fetchWorkspaceGuide).not.toHaveBeenCalled()
  })

  it('undoes a region line with the workspace guide pinned', async () => {
    serve(changesFeed)
    const wrapper = await mountPanel()
    await wrapper.findAll('.mch-row')[0].get('button').trigger('click')
    await flushPromises()
    expect(fetchWorkspaceGuide).toHaveBeenCalledWith(CHANGES_WORKSPACE)
    const [, options] = startFileDiscussion.mock.calls[0] as unknown as [unknown, { path: string; seed: string }]
    expect(options.path).toBe('storybook-alpha/AGENTS.md')
    expect(options.seed).toContain('ciao memory update')
  })

  it('pins the undo log itself when the entry names no file it can open', async () => {
    const entry = { ...changesFeed.groups[1].entries[0], path: '', open_path: '', target: '../outside.md' }
    serve({ ...changesFeed, groups: [{ ...changesFeed.groups[1], entries: [entry] }] })
    const wrapper = await mountPanel()
    await wrapper.get('.mch-undo').trigger('click')
    await flushPromises()
    const [, options] = startFileDiscussion.mock.calls[0] as unknown as [unknown, { path: string }]
    expect(options.path).toBe(changesFeed.undo_log_path)
  })

  it('opens a skill suggestion in the care page review', async () => {
    serve(changesFeed)
    const wrapper = await mountPanel()
    await wrapper.findAll('button').find(b => b.text() === 'Review')!.trigger('click')
    await flushPromises()
    expect(wrapper.router.currentRoute.value.fullPath).toBe('/memory/care?proposal=Workspace/Proposals/shorten-weekly-summary.md')
  })

  it('shows model-written text as text, never as markup', async () => {
    const hostile = '<img src=x onerror="alert(1)"> **bold**'
    const entry = { ...changesFeed.groups[1].entries[0], quote: hostile, why: '<script>x</script>' }
    serve({ ...changesFeed, groups: [{ ...changesFeed.groups[1], entries: [entry] }] })
    const wrapper = await mountPanel()
    expect(wrapper.find('img').exists()).toBe(false)
    expect(wrapper.find('script').exists()).toBe(false)
    expect(wrapper.get('.mch-sentence').text()).toContain(hostile)
    expect(wrapper.get('.mch-source').text()).toContain('<script>x</script>')
  })

  it('says what an empty, unreadable or failed read means', async () => {
    serve(emptyChangesFeed)
    expect((await mountPanel()).get('.mch-empty').text()).toContain('Nothing recorded yet.')

    setActivePinia(createPinia())
    useProjectStore().activeWorkspace = CHANGES_WORKSPACE
    vi.restoreAllMocks()
    serve({ ...emptyChangesFeed, undo_log: 'unreadable' })
    expect((await mountPanel()).text()).toContain('could not be read safely')

    setActivePinia(createPinia())
    useProjectStore().activeWorkspace = CHANGES_WORKSPACE
    vi.restoreAllMocks()
    serve(new Error('engine offline'))
    const failed = await mountPanel()
    expect(failed.get('[role="alert"]').text()).toContain('Could not read the changes: engine offline')
    expect(failed.get('.mch-retry').attributes('type')).toBe('button')
  })

  it('keeps every control a real button of its own type', async () => {
    serve(changesFeed)
    const wrapper = await mountPanel()
    for (const control of wrapper.findAll('button')) {
      expect(control.attributes('type')).toBe('button')
      ;(control.element as HTMLButtonElement).focus()
      expect(document.activeElement).toBe(control.element)
    }
  })
})

describe('MemoryChangesRail', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    useProjectStore().activeWorkspace = CHANGES_WORKSPACE
  })
  afterEach(() => vi.restoreAllMocks())

  it('counts the week in the docked window', async () => {
    serve(changesFeed)
    const wrapper = await mountPanel(MemoryChangesPanel)
    const rail = mount(MemoryChangesRail, { global: { plugins: [wrapper.router] } })
    await flushPromises()
    expect(rail.get('.rail-title').text()).toBe('This week')
    expect(rail.findAll('.changes-rail-kv').map(r => r.text())).toEqual([
      'Lines replaced or removed3', 'In always-loaded memory1', 'In notes2', 'Skill suggestions1', 'Chats read for memory2',
    ])
    expect(rail.text()).toContain('Lines Ciaobot adds are not logged')
  })
})
