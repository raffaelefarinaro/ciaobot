// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'
import { useFileViewerStore } from '../../stores/fileViewer'

function timestamp(secondsAgo: number): string {
  return new Date(Date.now() - secondsAgo * 1000).toISOString()
}

function seedChats(includeChats = true) {
  const store = useProjectStore()
  store.workspaces = [
    { name: 'personal', vault_root: '', default_provider: 'claude', default_model: '', gws_profile: '', color: 'pink' },
    { name: 'work', vault_root: '', default_provider: 'claude', default_model: '', gws_profile: '', color: 'cyan' },
  ]
  store.projects = [
    { project_id: 'personal-project', name: 'Personal project', workspace: 'personal' },
    { project_id: 'personal-general', name: 'General', workspace: 'personal' },
    { project_id: 'work-project', name: 'Work project', workspace: 'work' },
    { project_id: 'work-general', name: 'General', workspace: 'work' },
  ] as unknown as typeof store.projects
  store.chats = includeChats ? [
    {
      chat_id: 'needs', project_id: 'personal-project', title: 'Needs an answer',
      pending_question: JSON.stringify({ questions: [{ question: 'Which launch date should we use?' }] }),
      created_at: timestamp(60 * 60), last_activity_at: timestamp(60 * 60), last_read_at: timestamp(60 * 60), archived: false, local: true,
    },
    {
      chat_id: 'working', project_id: 'work-project', title: 'Background work',
      created_at: timestamp(2 * 60 * 60), last_activity_at: timestamp(2 * 60 * 60), last_read_at: timestamp(2 * 60 * 60), archived: false, local: true,
    },
    {
      chat_id: 'quiet', project_id: 'personal-project', title: 'A quiet chat',
      created_at: timestamp(2 * 24 * 60 * 60), last_activity_at: timestamp(2 * 24 * 60 * 60), last_read_at: timestamp(2 * 24 * 60 * 60), archived: false, local: true,
    },
    {
      chat_id: 'older', project_id: 'work-project', title: 'An older chat',
      created_at: timestamp(8 * 24 * 60 * 60), last_activity_at: timestamp(8 * 24 * 60 * 60), last_read_at: timestamp(8 * 24 * 60 * 60), archived: false, local: true,
    },
  ] as unknown as typeof store.chats : [] as unknown as typeof store.chats
  store.activeWorkspace = 'personal'
  store.bootstrapped = true
  store.projectStreaming = {}
  store.backgroundAgents = { working: 1 }
  return store
}

async function mountHome(includeChats = true) {
  seedChats(includeChats)
  const taskStore = useTaskStore()
  taskStore.loops = [] as unknown as typeof taskStore.loops
  const { default: HomeRecentChats } = await import('../HomeRecentChats.vue')
  const wrapper = mount(HomeRecentChats, { attachTo: document.body })
  await nextTick()
  return wrapper
}

describe('HomeRecentChats lanes and tiers', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.restoreAllMocks()
    if (!Element.prototype.scrollIntoView) Element.prototype.scrollIntoView = () => {}
  })

  it('renders only the active workspace lane', async () => {
    const wrapper = await mountHome()
    expect(wrapper.findAll('.home-lane')).toHaveLength(1)
    expect(wrapper.find('.home-lane-name').text()).toBe('personal')
    expect(wrapper.find('[data-lane-key="personal"]').exists()).toBe(true)
    expect(wrapper.find('[data-lane-key="work"]').exists()).toBe(false)
    wrapper.unmount()
  })

  // The point of the scoped home: the workspace toggle swaps the lane's
  // content instead of revealing another column.
  it('swaps the lane content when the active workspace changes', async () => {
    seedChats()
    const store = useProjectStore()
    const taskStore = useTaskStore()
    taskStore.loops = [] as unknown as typeof taskStore.loops
    const { default: HomeRecentChats } = await import('../HomeRecentChats.vue')
    const wrapper = mount(HomeRecentChats, { attachTo: document.body })
    await nextTick()

    expect(wrapper.find('[data-lane-key="personal"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('Needs an answer')

    // What the workspace toggle in the sidebar drives.
    store.activeWorkspace = 'work'
    await nextTick()

    expect(wrapper.find('[data-lane-key="work"]').exists()).toBe(true)
    expect(wrapper.findAll('.home-lane')).toHaveLength(1)
    const titles = wrapper.findAll('.home-chat-title').map(n => n.text())
    expect(titles).toContain('Background work')
    expect(titles).not.toContain('Needs an answer')
    wrapper.unmount()
  })

  it('hides other workspaces chats until they are switched to', async () => {
    const wrapper = await mountHome()
    const titles = wrapper.findAll('.home-chat-title').map(n => n.text())
    expect(titles).toContain('Needs an answer')
    expect(titles).toContain('A quiet chat')
    expect(titles).not.toContain('Background work')
    expect(titles).not.toContain('An older chat')
    wrapper.unmount()
  })

  it('assigns chats to one priority tier and shows the pending question', async () => {
    const wrapper = await mountHome()
    expect(wrapper.find('.home-tier--needsYou .home-chat-title').text()).toBe('Needs an answer')
    expect(wrapper.find('.home-chat-question').text()).toContain('Which launch date')
    // The work chat sits in another workspace, so no working tier renders here.
    expect(wrapper.find('.home-tier--working').exists()).toBe(false)
    expect(wrapper.find('.home-tier--quiet .home-chat-title').text()).toBe('A quiet chat')
    // Older chats are listed in quiet now, not split behind a disclosure, so
    // every seeded chat renders as a row.
    expect(wrapper.find('.home-lane-older-toggle').exists()).toBe(false)
    expect(wrapper.findAll('.home-chat-item')).toHaveLength(2)
  })

  it('lists older chats inline with quiet instead of behind a disclosure', async () => {
    const store = seedChats()
    // The seeded old chat lives in the other workspace; pull it into the
    // active one so the tier shape is what is under test here.
    store.chats = store.chats.map(chat =>
      chat.chat_id === 'older' ? { ...chat, project_id: 'personal-project' } : chat,
    ) as unknown as typeof store.chats
    const taskStore = useTaskStore()
    taskStore.loops = [] as unknown as typeof taskStore.loops
    const { default: HomeRecentChats } = await import('../HomeRecentChats.vue')
    const wrapper = mount(HomeRecentChats, { attachTo: document.body })
    await nextTick()
    const quietTitles = wrapper.findAll('.home-tier--quiet .home-chat-title').map(n => n.text())
    expect(quietTitles).toContain('A quiet chat')
    expect(quietTitles).toContain('An older chat')
    expect(wrapper.find('.home-tier--older').exists()).toBe(false)
    wrapper.unmount()
  })

  it('hides the needs-you tier entirely when nothing needs the user', async () => {
    const store = seedChats()
    store.chats = store.chats.filter(chat => chat.chat_id !== 'needs')
    const { default: HomeRecentChats } = await import('../HomeRecentChats.vue')
    const wrapper = mount(HomeRecentChats)
    // Previously rendered an empty tier plus "// nothing needs you here" in
    // every lane, which meant the loudest label on screen was usually a
    // statement that there was nothing to do.
    expect(wrapper.findAll('.home-tier--needsYou')).toHaveLength(0)
    expect(wrapper.text()).not.toContain('nothing needs you here')
  })

  it('keeps tier labels lowercase', async () => {
    const wrapper = await mountHome()
    const labels = wrapper.findAll('.home-tier-label').map(n => n.text())
    expect(labels).toContain('needs you')
    expect(labels).toContain('quiet')
    expect(labels.some(l => l === l.toUpperCase() && /[A-Z]/.test(l))).toBe(false)
  })

  it('lists archived chats being tidied in their lane with the live step', async () => {
    const store = seedChats()
    // The tidying chats live in the work workspace, so look at work.
    store.activeWorkspace = 'work'
    store.chats = [
      ...store.chats,
      {
        chat_id: 'tidy', project_id: 'work-project', title: 'Archived work chat',
        created_at: timestamp(300), last_activity_at: timestamp(300), last_read_at: timestamp(300),
        archived: true, local: true, archive_path: 'archive/tidy.md',
        postprocess: { state: 'running', step: 'insights', expected: [], steps: {} },
      },
      {
        chat_id: 'tidy-no-file', project_id: 'work-project', title: 'Archived without file',
        created_at: timestamp(600), last_activity_at: timestamp(600), last_read_at: timestamp(600),
        archived: true, local: true,
        postprocess: { state: 'running', step: 'project_doc_update', expected: [], steps: {} },
      },
    ] as unknown as typeof store.chats
    const viewer = useFileViewerStore()
    const openSpy = vi.spyOn(viewer, 'open').mockResolvedValue(true)
    const taskStore = useTaskStore()
    taskStore.loops = [] as unknown as typeof taskStore.loops
    const { default: HomeRecentChats } = await import('../HomeRecentChats.vue')
    const wrapper = mount(HomeRecentChats, { attachTo: document.body })
    await nextTick()

    // The lane carries its own tidying tier, and the header count has rows
    // behind it.
    const workLane = wrapper.find('[data-lane-key="work"]')
    const tidyRows = workLane.findAll('.home-tier--tidying .home-chat-item')
    expect(tidyRows).toHaveLength(2)
    expect(workLane.find('.home-tier--tidying .home-tier-label').text()).toBe('tidying up')
    expect(workLane.find('.home-lane-summary').text()).toContain('2 tidying up')
    const tidyTitles = tidyRows.map(row => row.find('.home-chat-title').text())
    expect(tidyTitles).toContain('Archived work chat')
    expect(tidyTitles).toContain('Archived without file')

    const withFileRow = tidyRows.find(row => row.find('.home-chat-title').text() === 'Archived work chat')!
    expect(withFileRow.text()).toContain('extracting insights')

    // A tidying chat without an archive file has nothing to open.
    const noFileRow = tidyRows.find(row => row.find('.home-chat-title').text() === 'Archived without file')!
    expect(noFileRow.text()).toContain('folding into project doc')
    expect((noFileRow.element as HTMLButtonElement).disabled).toBe(true)

    // The other workspace's chats stay hidden until it is switched to.
    expect(wrapper.text()).not.toContain('Needs an answer')

    // Archived chats stay out of the priority tiers; the tidying row lives
    // only under the lane's own tidying tier.
    const priorityTitles = wrapper.findAll(
      '.home-tier--needsYou .home-chat-title, .home-tier--working .home-chat-title, .home-tier--unread .home-chat-title, .home-tier--quiet .home-chat-title',
    ).map(n => n.text())
    expect(priorityTitles).not.toContain('Archived work chat')
    expect(priorityTitles).not.toContain('Archived without file')

    // Clicking a row opens the archived transcript in the file viewer.
    await withFileRow.trigger('click')
    expect(openSpy).toHaveBeenCalledWith('archive/tidy.md')
    wrapper.unmount()
  })

  it('keeps the tidying section visible when no active chats remain', async () => {
    const store = seedChats(false)
    store.activeWorkspace = 'work'
    store.chats = [{
      chat_id: 'only-tidy', project_id: 'work-project', title: 'Archived work chat',
      created_at: timestamp(300), last_activity_at: timestamp(300), last_read_at: timestamp(300),
      archived: true, local: true, archive_path: 'archive/only-tidy.md',
      postprocess: { state: 'running', step: 'insights', expected: [], steps: {} },
    }] as unknown as typeof store.chats
    const { default: HomeRecentChats } = await import('../HomeRecentChats.vue')
    const wrapper = mount(HomeRecentChats, { attachTo: document.body })
    await nextTick()

    expect(wrapper.find('.home-recent').exists()).toBe(true)
    const workLane = wrapper.find('[data-lane-key="work"]')
    expect(workLane.find('.home-tier--tidying').exists()).toBe(true)
    expect(workLane.find('.home-tier--tidying .home-chat-title').text()).toBe('Archived work chat')
    expect(workLane.find('.home-chat-tidy-note').text()).toContain('extracting insights')
    wrapper.unmount()
  })

  it('lists failed insights in their lane with a retry button and header count', async () => {
    const store = seedChats()
    // The failed chat lives in the work workspace, so look at work.
    store.activeWorkspace = 'work'
    store.chats = [
      ...store.chats,
      {
        chat_id: 'c-failed-work', project_id: 'work-project', title: 'Failed work chat',
        created_at: timestamp(300), last_activity_at: timestamp(300), last_read_at: timestamp(300),
        archived: true, local: true, archive_path: 'archive/failed.md',
        postprocess: { state: 'done', steps: { insights: { status: 'error' } } },
      },
      {
        chat_id: 'c-ok-personal', project_id: 'personal-project', title: 'Ok personal chat',
        created_at: timestamp(600), last_activity_at: timestamp(600), last_read_at: timestamp(600),
        archived: true, local: true,
        postprocess: { state: 'done', steps: { insights: { status: 'ok' } } },
      },
    ] as unknown as typeof store.chats
    const { default: HomeRecentChats } = await import('../HomeRecentChats.vue')
    const wrapper = mount(HomeRecentChats, { attachTo: document.body })
    await nextTick()

    // Only the active workspace's lane renders at all.
    expect(wrapper.findAll('.home-lane')).toHaveLength(1)
    const workLane = wrapper.find('[data-lane-key="work"]')
    const failedRows = workLane.findAll('.home-tier--failed .home-chat-item')
    expect(failedRows).toHaveLength(1)
    expect(failedRows[0].find('.home-chat-title').text()).toBe('Failed work chat')
    expect(failedRows[0].find('.home-chat-tidy-note').text()).toBe('insights failed')
    expect(failedRows[0].find('.home-chat-retry').exists()).toBe(true)
    // Header carries the recovery count in the warn register.
    expect(workLane.find('.home-lane-summary').text()).toContain('1 insights failed')
    wrapper.unmount()
  })

  it('keeps the failed-insights section visible when no active chats remain', async () => {
    const store = seedChats(false)
    store.activeWorkspace = 'work'
    store.chats = [{
      chat_id: 'only-failed', project_id: 'work-project', title: 'Failed chat',
      created_at: timestamp(300), last_activity_at: timestamp(300), last_read_at: timestamp(300),
      archived: true, local: true, archive_path: 'archive/only-failed.md',
      postprocess: { state: 'done', steps: { insights: { status: 'error' } } },
    }] as unknown as typeof store.chats
    const { default: HomeRecentChats } = await import('../HomeRecentChats.vue')
    const wrapper = mount(HomeRecentChats, { attachTo: document.body })
    await nextTick()

    expect(wrapper.find('.home-recent').exists()).toBe(true)
    const workLane = wrapper.find('[data-lane-key="work"]')
    expect(workLane.find('.home-tier--failed').exists()).toBe(true)
    expect(workLane.find('.home-tier--failed .home-chat-title').text()).toBe('Failed chat')
    wrapper.unmount()
  })

  // One lane on screen: up/down roams its rows, left/right has nowhere to go
  // and is consumed so ChatLayout's global handler does not roam elsewhere.
  it('roams the single lane vertically and consumes sideways arrows at its edge', async () => {
    const wrapper = await mountHome()
    const vm = wrapper.vm as unknown as { onArrow: (key: string) => boolean }
    const cards = wrapper.findAll('.home-chat-item')

    expect(vm.onArrow('ArrowDown')).toBe(true)
    expect(document.activeElement).toBe(cards[0].element)
    expect(vm.onArrow('ArrowDown')).toBe(true)
    expect(document.activeElement).toBe(cards[1].element)
    expect(vm.onArrow('ArrowUp')).toBe(true)
    expect(document.activeElement).toBe(cards[0].element)
    // No second lane to move to; the key is still consumed.
    expect(vm.onArrow('ArrowRight')).toBe(true)
    expect(document.activeElement).toBe(cards[0].element)
    expect(vm.onArrow('ArrowLeft')).toBe(true)
    expect(document.activeElement).toBe(cards[0].element)
  })

  // Rescue lanes (stale or unknown workspaces) stack beneath the selected
  // workspace's lane, so vertical motion crosses between them while horizontal
  // motion stays within one.
  it('crosses into stacked rescue lanes vertically and roams them horizontally', async () => {
    const store = seedChats()
    // A chat whose project vanished renders in an unknown-workspace rescue
    // lane below the active workspace's lane.
    store.projects = store.projects.filter(project => project.project_id !== 'work-project')
    store.chats = [
      ...store.chats,
      {
        chat_id: 'orphan', project_id: 'work-project', title: 'An orphaned chat',
        created_at: timestamp(90), last_activity_at: timestamp(90), last_read_at: timestamp(90), archived: false, local: true,
      },
    ] as unknown as typeof store.chats
    const taskStore = useTaskStore()
    taskStore.loops = [] as unknown as typeof taskStore.loops
    const { default: HomeRecentChats } = await import('../HomeRecentChats.vue')
    const wrapper = mount(HomeRecentChats, { attachTo: document.body })
    await nextTick()
    const vm = wrapper.vm as unknown as { onArrow: (key: string) => boolean }
    const lanes = wrapper.findAll('.home-lane')
    expect(lanes).toHaveLength(2)

    const laneRects = [
      { top: 0, left: 0, width: 600, height: 300 },
      { top: 320, left: 0, width: 600, height: 300 },
    ]
    lanes.forEach((lane, index) => {
      vi.spyOn(lane.element, 'getBoundingClientRect').mockReturnValue({
        ...laneRects[index],
        right: laneRects[index].left + laneRects[index].width,
        bottom: laneRects[index].top + laneRects[index].height,
        x: laneRects[index].left,
        y: laneRects[index].top,
        toJSON: () => ({}),
      })
    })

    const personalCards = lanes[0].findAll('.home-chat-item')
    const orphanCards = lanes[1].findAll('.home-chat-item')

    expect(vm.onArrow('ArrowDown')).toBe(true)
    expect(document.activeElement).toBe(personalCards[0].element)
    expect(vm.onArrow('ArrowDown')).toBe(true)
    expect(document.activeElement).toBe(orphanCards[0].element)
    // Stacked lanes: sideways roams within a lane.
    expect(vm.onArrow('ArrowRight')).toBe(true)
    expect(document.activeElement).toBe(orphanCards[1].element)
    expect(vm.onArrow('ArrowLeft')).toBe(true)
    expect(document.activeElement).toBe(orphanCards[0].element)
    expect(vm.onArrow('ArrowUp')).toBe(true)
    expect(document.activeElement).toBe(personalCards[0].element)
    wrapper.unmount()
  })

  it('consumes arrow keys at edges and reports no navigation without chats', async () => {
    const wrapper = await mountHome()
    const vm = wrapper.vm as unknown as { onArrow: (key: string) => boolean }
    vm.onArrow('ArrowDown')
    expect(vm.onArrow('ArrowUp')).toBe(true)

    const empty = await mountHome(false)
    const emptyVm = empty.vm as unknown as { onArrow: (key: string) => boolean }
    expect(emptyVm.onArrow('ArrowDown')).toBe(false)
  })

  it('makes quiet rows focusable buttons', async () => {
    const wrapper = await mountHome()
    expect(wrapper.find('.home-tier--quiet .home-chat-item').element.tagName).toBe('BUTTON')
  })

  // Regression: with focus on the body (empty-space click, or Esc back out of
  // a chat), the first arrow used to jump to whichever lane came first in DOM
  // order rather than the workspace the user is actually looking at.
  it('anchors the first arrow press to the active workspace lane', async () => {
    const store = seedChats()
    store.activeWorkspace = 'work'
    const taskStore = useTaskStore()
    taskStore.loops = [] as unknown as typeof taskStore.loops
    const { default: HomeRecentChats } = await import('../HomeRecentChats.vue')
    const wrapper = mount(HomeRecentChats, { attachTo: document.body })
    await nextTick()
    const vm = wrapper.vm as unknown as { onArrow: (key: string) => boolean }

    expect(vm.onArrow('ArrowDown')).toBe(true)
    const workCards = wrapper.find('[data-lane-key="work"]').findAll('.home-chat-item')
    expect(document.activeElement).toBe(workCards[0].element)
    wrapper.unmount()
  })

  // Regression: focus landing on a lane's "+ new" header control (via Tab or a
  // click) used to make the next arrow jump to the first lane. It now stays in
  // the lane that holds the focused control.
  it('keeps arrows in the lane whose header control has focus', async () => {
    const wrapper = await mountHome()
    const vm = wrapper.vm as unknown as { onArrow: (key: string) => boolean }

    const personalNew = wrapper.find('[data-lane-key="personal"] .home-lane-new').element as HTMLElement
    personalNew.focus()
    expect(document.activeElement).toBe(personalNew)

    expect(vm.onArrow('ArrowDown')).toBe(true)
    const personalCards = wrapper.find('[data-lane-key="personal"]').findAll('.home-chat-item')
    expect(document.activeElement).toBe(personalCards[0].element)
    wrapper.unmount()
  })
})


// Regression coverage for defects the lane rewrite introduced. The original
// suite asserted lane counts but had no assertion that every non-archived chat
// still reaches the screen, which is how these got through.
describe('HomeRecentChats regressions', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.restoreAllMocks()
    if (!Element.prototype.scrollIntoView) Element.prototype.scrollIntoView = () => {}
  })

  it('still renders chats whose workspace is missing from workspaceOptions', async () => {
    const store = seedChats()
    // What a workspace rename or delete leaves behind: workspaces.value is
    // refreshed, projects.value[].workspace keeps the stale name.
    store.projects = [
      { project_id: 'personal-project', name: 'Personal project', workspace: 'renamed-away' },
    ] as unknown as typeof store.projects
    const taskStore = useTaskStore()
    taskStore.loops = [] as unknown as typeof taskStore.loops
    const { default: HomeRecentChats } = await import('../HomeRecentChats.vue')
    const wrapper = mount(HomeRecentChats, { attachTo: document.body })
    await nextTick()

    const titles = wrapper.findAll('.home-chat-title').map(n => n.text())
    expect(titles).toContain('Needs an answer')
    expect(titles).toContain('A quiet chat')
    wrapper.unmount()
  })

  it('does not print a quiet count and "all quiet" together', async () => {
    const wrapper = await mountHome()
    for (const summary of wrapper.findAll('.home-lane-summary')) {
      expect(summary.text()).not.toMatch(/quiet\s+all quiet/)
    }
    wrapper.unmount()
  })

  // workspaceNeedsInput() counts nested delegates; activeChatsAll excludes
  // them, so the header used to claim a chat needed you with no row to click.
  it('keeps the lane needs-you count equal to the rows rendered', async () => {
    const store = seedChats()
    store.chats = [
      ...store.chats,
      {
        chat_id: 'delegate', project_id: 'personal-project', title: 'Internal delegate',
        spawned_from_chat_id: 'quiet',
        pending_question: JSON.stringify({ questions: [{ question: 'Internal?' }] }),
        created_at: timestamp(30), last_activity_at: timestamp(30), last_read_at: timestamp(30), archived: false, local: true,
      },
    ] as unknown as typeof store.chats
    const taskStore = useTaskStore()
    taskStore.loops = [] as unknown as typeof taskStore.loops
    const { default: HomeRecentChats } = await import('../HomeRecentChats.vue')
    const wrapper = mount(HomeRecentChats, { attachTo: document.body })
    await nextTick()

    const personalLane = wrapper.find('[data-lane-key="personal"]')
    const summary = personalLane.find('.home-lane-summary').text()
    const rows = personalLane.findAll('.home-tier--needsYou .home-chat-item').length
    const claimed = Number(/(\d+)\s+need/.exec(summary)?.[1] ?? 0)
    expect(claimed).toBe(rows)
    wrapper.unmount()
  })
})

describe('HomeRecentChats new-chat project picker', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.restoreAllMocks()
    if (!Element.prototype.scrollIntoView) Element.prototype.scrollIntoView = () => {}
  })

  it('keeps "+ new" one click to General and offers the rest behind the caret', async () => {
    const wrapper = await mountHome()
    const lane = wrapper.find('[data-lane-key="personal"]')

    await lane.find('.home-lane-new').trigger('click')
    expect(wrapper.emitted('new-workspace-chat')?.[0]).toEqual([
      { workspace: 'personal', projectId: 'personal-general', isCreating: false },
    ])

    await lane.find('.home-lane-new-caret').trigger('click')
    // General leads: it is what the plain button creates in.
    expect(lane.findAll('.home-lane-project-option').map(n => n.text())).toEqual([
      'General',
      'Personal project',
    ])
    wrapper.unmount()
  })

  it('creates in the picked project and closes the menu', async () => {
    const wrapper = await mountHome()
    const lane = wrapper.find('[data-lane-key="personal"]')
    await lane.find('.home-lane-new-caret').trigger('click')

    const options = lane.findAll('.home-lane-project-option')
    await options[1].trigger('click')

    expect(wrapper.emitted('new-workspace-chat')?.[0]).toEqual([
      { workspace: 'personal', projectId: 'personal-project', isCreating: false },
    ])
    expect(lane.find('.home-lane-project-menu').exists()).toBe(false)
    wrapper.unmount()
  })

  // Hover does not exist on a phone, so the caret is dimmed rather than hidden
  // and has to stay reachable by keyboard: Esc must hand focus back rather than
  // dropping it on the body.
  it('closes on Escape and returns focus to the caret', async () => {
    const wrapper = await mountHome()
    const lane = wrapper.find('[data-lane-key="personal"]')
    const caret = lane.find('.home-lane-new-caret')
    await caret.trigger('click')
    await nextTick()

    await lane.find('.home-lane-project-menu').trigger('keydown.esc')
    await nextTick()

    expect(lane.find('.home-lane-project-menu').exists()).toBe(false)
    expect(document.activeElement).toBe(caret.element)
    wrapper.unmount()
  })

  // ChatLayout binds arrows on window to roam the chat grid, and defers to any
  // key a nested popup already consumed. The menu therefore has to mark arrows
  // handled, or focus lands on a chat card while the menu stays open — and
  // Enter then opens an unrelated chat.
  it('marks arrow keys handled so the chat grid does not roam', async () => {
    const wrapper = await mountHome()
    const lane = wrapper.find('[data-lane-key="personal"]')
    await lane.find('.home-lane-new-caret').trigger('click')
    await nextTick()

    const options = lane.findAll('.home-lane-project-option')
    expect(document.activeElement).toBe(options[0].element)

    const seen: KeyboardEvent[] = []
    const spy = (event: Event) => seen.push(event as KeyboardEvent)
    window.addEventListener('keydown', spy)
    await lane.find('.home-lane-project-menu').trigger('keydown.down')
    window.removeEventListener('keydown', spy)

    expect(document.activeElement).toBe(options[1].element)
    expect(seen.at(-1)?.defaultPrevented).toBe(true)
    wrapper.unmount()
  })

  it('returns focus to the caret after picking a project', async () => {
    const wrapper = await mountHome()
    const lane = wrapper.find('[data-lane-key="personal"]')
    const caret = lane.find('.home-lane-new-caret')
    await caret.trigger('click')
    await nextTick()

    await lane.findAll('.home-lane-project-option')[1].trigger('click')
    await nextTick()

    expect(document.activeElement).toBe(caret.element)
    wrapper.unmount()
  })

  it('hides the caret when the workspace has only one project', async () => {
    const store = seedChats()
    store.projects = store.projects.filter(
      project => project.project_id !== 'personal-project',
    )
    store.chats = store.chats.map(chat =>
      chat.project_id === 'personal-project'
        ? { ...chat, project_id: 'personal-general' }
        : chat,
    ) as unknown as typeof store.chats
    const taskStore = useTaskStore()
    taskStore.loops = [] as unknown as typeof taskStore.loops
    const { default: HomeRecentChats } = await import('../HomeRecentChats.vue')
    const wrapper = mount(HomeRecentChats, { attachTo: document.body })
    await nextTick()

    const lane = wrapper.find('[data-lane-key="personal"]')
    expect(lane.find('.home-lane-new').exists()).toBe(true)
    expect(lane.find('.home-lane-new-caret').exists()).toBe(false)
    wrapper.unmount()
  })
})
