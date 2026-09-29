// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { useProjectStore } from '../../stores/projects'
import { useFileViewerStore } from '../../stores/fileViewer'

function timestamp(secondsAgo: number): string {
  return new Date(Date.now() - secondsAgo * 1000).toISOString()
}

function seedChats(includeChats = true) {
  const store = useProjectStore()
  store.workspaces = [
    { name: 'personal', vault_root: '', default_provider: 'claude', gws_profile: '', color: 'pink' },
    { name: 'work', vault_root: '', default_provider: 'claude', gws_profile: '', color: 'cyan' },
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
    // The sidebar scope names the workspace; the lane does not repeat it.
    expect(wrapper.find('.home-lane-name').exists()).toBe(false)
    expect(wrapper.find('[data-lane-key="personal"]').exists()).toBe(true)
    expect(wrapper.find('[data-lane-key="work"]').exists()).toBe(false)
    wrapper.unmount()
  })

  // The point of the scoped home: the workspace toggle swaps the lane's
  // content instead of revealing another column.
  it('swaps the lane content when the active workspace changes', async () => {
    seedChats()
    const store = useProjectStore()
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

  it('gives every row a project and status sub-line read from its tier', async () => {
    const wrapper = await mountHome()
    const needs = wrapper.find('.home-tier--needsYou .home-chat-item')
    expect(needs.find('.home-chat-project').text()).toBe('Personal project')
    expect(needs.find('.home-chat-status').text()).toBe('waiting for you')
    expect(wrapper.find('.home-tier--quiet .home-chat-status').text()).toBe('no new activity')

    // The work chat has a background agent running, which is what puts it in
    // the working tier; the sub-line says so rather than inventing activity.
    useProjectStore().activeWorkspace = 'work'
    await nextTick()
    expect(wrapper.find('.home-tier--working .home-chat-status').text()).toBe('agent is working')
    wrapper.unmount()
  })

  it('lists older chats inline with quiet instead of behind a disclosure', async () => {
    const store = seedChats()
    // The seeded old chat lives in the other workspace; pull it into the
    // active one so the tier shape is what is under test here.
    store.chats = store.chats.map(chat =>
      chat.chat_id === 'older' ? { ...chat, project_id: 'personal-project' } : chat,
    ) as unknown as typeof store.chats
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
    expect(labels).toContain('earlier')
    expect(labels.some(l => l === l.toUpperCase() && /[A-Z]/.test(l))).toBe(false)
  })

  // The whole point of the memory-insight section: one archived conversation
  // gets ONE entry, the pass is not a chat row in the tiers, and clicking the
  // entry opens the pass — the chat with the live turn in it.
  it('shows one memory-insight row per archived conversation and opens the pass', async () => {
    const store = seedChats()
    store.activeWorkspace = 'work'
    store.chats = [
      ...store.chats,
      {
        chat_id: 'src', project_id: 'work-project', title: 'Archived work chat',
        created_at: timestamp(300), last_activity_at: timestamp(300), last_read_at: timestamp(300),
        archived: true, local: true, archive_path: 'archive/src.md',
        postprocess: { steps: { memory_pass: { status: 'running', extra: { chat_id: 'pass-1' } } } },
      },
      {
        chat_id: 'pass-1', project_id: 'work-project', title: 'Memory pass · Archived work chat',
        created_at: timestamp(120), last_activity_at: timestamp(120), last_read_at: timestamp(300),
        archived: false, local: true,
        helper: {
          kind: 'memory_pass', state: 'running', source_chat_id: 'src', archive_path: 'archive/src.md',
          doc_path: '', source_title: 'Archived work chat', source_project: '', archive_policy: 'when_clean',
        },
      },
    ] as unknown as typeof store.chats

    const switchSpy = vi.spyOn(store, 'switchChat').mockResolvedValue(undefined)
    const viewer = useFileViewerStore()
    const openSpy = vi.spyOn(viewer, 'open').mockResolvedValue(true)
    const { default: HomeRecentChats } = await import('../HomeRecentChats.vue')
    const wrapper = mount(HomeRecentChats, { attachTo: document.body })
    await nextTick()

    const workLane = wrapper.find('[data-lane-key="work"]')
    const rows = workLane.findAll('.home-insight-row')
    expect(rows).toHaveLength(1)
    expect(workLane.find('.home-insights .home-tier-label').text()).toBe('memory insights')
    // The row is named for the conversation, not for the pass's internal title.
    expect(rows[0].find('.home-chat-title').text()).toBe('Archived work chat')
    expect(rows[0].find('.home-chat-tidy-note').text()).toBe('updating memory…')

    // The pass is a chat, so it used to be a Working row of its own. It is not
    // in the tiers any more, and the same conversation is not listed twice.
    const tierTitles = workLane.findAll('.home-tier .home-chat-title').map(n => n.text())
    expect(tierTitles).not.toContain('Memory pass · Archived work chat')
    expect(workLane.findAll('.home-insight-row')).toHaveLength(1)

    await rows[0].find('.home-chat-item').trigger('click')
    expect(switchSpy).toHaveBeenCalledWith('pass-1')
    expect(openSpy).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('falls back to the archived transcript while no pass exists yet', async () => {
    const store = seedChats()
    store.activeWorkspace = 'work'
    store.chats = [
      ...store.chats,
      {
        chat_id: 'tidy', project_id: 'work-project', title: 'Archived work chat',
        created_at: timestamp(300), last_activity_at: timestamp(300), last_read_at: timestamp(300),
        archived: true, local: true, archive_path: 'archive/tidy.md',
      },
      {
        chat_id: 'tidy-no-file', project_id: 'work-project', title: 'Archived without file',
        created_at: timestamp(600), last_activity_at: timestamp(600), last_read_at: timestamp(600),
        archived: true, local: true,
      },
    ] as unknown as typeof store.chats
    store.archivingChats = { tidy: true, 'tidy-no-file': true }
    const switchSpy = vi.spyOn(store, 'switchChat').mockResolvedValue(undefined)
    const viewer = useFileViewerStore()
    const openSpy = vi.spyOn(viewer, 'open').mockResolvedValue(true)
    const { default: HomeRecentChats } = await import('../HomeRecentChats.vue')
    const wrapper = mount(HomeRecentChats, { attachTo: document.body })
    await nextTick()

    const workLane = wrapper.find('[data-lane-key="work"]')
    const rows = workLane.findAll('.home-insight-row')
    expect(rows).toHaveLength(2)
    expect(workLane.find('.home-lane-status-text').text()).toContain('2 updating memory')
    expect(rows.map(row => row.find('.home-chat-tidy-note').text())).toEqual(['archiving…', 'archiving…'])

    // The other workspace's chats stay hidden until it is switched to.
    expect(wrapper.text()).not.toContain('Needs an answer')

    // Archived chats stay out of the priority tiers; this row lives only in the
    // insight section.
    const priorityTitles = wrapper.findAll(
      '.home-tier--needsYou .home-chat-title, .home-tier--working .home-chat-title, .home-tier--unread .home-chat-title, .home-tier--quiet .home-chat-title',
    ).map(n => n.text())
    expect(priorityTitles).not.toContain('Archived work chat')
    expect(priorityTitles).not.toContain('Archived without file')

    const tidyRow = rows.find(row => row.find('.home-chat-title').text() === 'Archived work chat')!
    await tidyRow.find('.home-chat-item').trigger('click')
    expect(openSpy).toHaveBeenCalledWith('archive/tidy.md')
    expect(switchSpy).not.toHaveBeenCalled()

    // An archive with no file and no pass is a dead entry, so the row is
    // disabled rather than a button that opens nothing.
    const noFileRow = rows.find(row => row.find('.home-chat-title').text() === 'Archived without file')!
    expect((noFileRow.find('.home-chat-item').element as HTMLButtonElement).disabled).toBe(true)
    wrapper.unmount()
  })

  it('keeps a pass blocked on the owner in the attention count with its question', async () => {
    const store = seedChats()
    store.activeWorkspace = 'work'
    store.chats = [
      ...store.chats,
      {
        chat_id: 'src', project_id: 'work-project', title: 'Archived work chat',
        created_at: timestamp(300), last_activity_at: timestamp(300), last_read_at: timestamp(300),
        archived: true, local: true, archive_path: 'archive/src.md',
      },
      {
        chat_id: 'pass-1', project_id: 'work-project', title: 'Memory pass · Archived work chat',
        created_at: timestamp(120), last_activity_at: timestamp(120), last_read_at: timestamp(300),
        archived: false, local: true,
        pending_question: JSON.stringify({ questions: [{ question: 'Which project is this?' }] }),
        helper: {
          kind: 'memory_pass', state: 'running', source_chat_id: 'src', archive_path: 'archive/src.md',
          doc_path: '', source_title: 'Archived work chat', source_project: '', archive_policy: 'when_clean',
        },
      },
    ] as unknown as typeof store.chats
    const { default: HomeRecentChats } = await import('../HomeRecentChats.vue')
    const wrapper = mount(HomeRecentChats, { attachTo: document.body })
    await nextTick()

    const workLane = wrapper.find('[data-lane-key="work"]')
    const row = workLane.find('.home-insight-row')
    expect(row.find('.home-chat-tidy-note').text()).toBe('needs you')
    expect(row.find('.home-chat-question').text()).toBe('Which project is this?')
    // One actionable item, and the sentence that says so has a row behind it.
    // The seeded work chat also has a background agent, which is the working
    // clause.
    expect(workLane.find('.home-lane-status-text').text())
      .toBe('1 chat needs your attention. 1 agent still working.')
    // A pass already waiting on you is not "still updating", so it is not
    // counted in the in-flight fragment either.
    expect(workLane.find('.home-lane-status-text').text()).not.toContain('updating memory')
    wrapper.unmount()
  })

  it('reports an unclean pass as needing attention on its own row', async () => {
    const store = seedChats()
    store.activeWorkspace = 'work'
    store.chats = [
      ...store.chats,
      {
        chat_id: 'src', project_id: 'work-project', title: 'Archived work chat',
        created_at: timestamp(300), last_activity_at: timestamp(300), last_read_at: timestamp(300),
        archived: true, local: true, archive_path: 'archive/src.md',
      },
      {
        chat_id: 'pass-1', project_id: 'work-project', title: 'Memory pass · Archived work chat',
        created_at: timestamp(120), last_activity_at: timestamp(120), last_read_at: timestamp(300),
        archived: false, local: true,
        helper: {
          kind: 'memory_pass', state: 'attention', source_chat_id: 'src', archive_path: 'archive/src.md',
          doc_path: '', source_title: 'Archived work chat', source_project: '', archive_policy: 'when_clean',
        },
      },
    ] as unknown as typeof store.chats
    const { default: HomeRecentChats } = await import('../HomeRecentChats.vue')
    const wrapper = mount(HomeRecentChats, { attachTo: document.body })
    await nextTick()

    const workLane = wrapper.find('[data-lane-key="work"]')
    expect(workLane.findAll('.home-insight-row')).toHaveLength(1)
    expect(workLane.find('.home-chat-tidy-note').text()).toBe('needs attention')
    expect(workLane.find('.home-lane-status-text').text())
      .toBe('1 chat needs your attention. 1 agent still working.')
    wrapper.unmount()
  })

  it('keeps the insight section visible when no active chats remain', async () => {
    const store = seedChats(false)
    store.activeWorkspace = 'work'
    store.chats = [{
      chat_id: 'only-archiving', project_id: 'work-project', title: 'Archiving chat',
      created_at: timestamp(300), last_activity_at: timestamp(300), last_read_at: timestamp(300),
      archived: true, local: true, archive_path: 'archive/only-archiving.md',
    }] as unknown as typeof store.chats
    store.archivingChats = { 'only-archiving': true }
    const { default: HomeRecentChats } = await import('../HomeRecentChats.vue')
    const wrapper = mount(HomeRecentChats, { attachTo: document.body })
    await nextTick()

    expect(wrapper.find('.home-recent').exists()).toBe(true)
    const workLane = wrapper.find('[data-lane-key="work"]')
    expect(workLane.find('.home-insights').exists()).toBe(true)
    expect(workLane.find('.home-insights .home-chat-title').text()).toBe('Archiving chat')
    expect(workLane.find('.home-lane-status-text').text())
      .toBe('nothing needs your attention. no agents working. 1 updating memory.')
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
    const { default: HomeRecentChats } = await import('../HomeRecentChats.vue')
    const wrapper = mount(HomeRecentChats, { attachTo: document.body })
    await nextTick()
    const vm = wrapper.vm as unknown as { onArrow: (key: string) => boolean }

    expect(vm.onArrow('ArrowDown')).toBe(true)
    const workCards = wrapper.find('[data-lane-key="work"]').findAll('.home-chat-item')
    expect(document.activeElement).toBe(workCards[0].element)
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
    for (const summary of wrapper.findAll('.home-lane-status-text')) {
      expect(summary.text()).not.toMatch(/quiet\s+all quiet/)
    }
    wrapper.unmount()
  })

  // The lane header is derived from the same chat set as the rows beneath it,
  // so it can never claim a chat needs you with no row to click.
  it('keeps the lane needs-you count equal to the rows rendered', async () => {
    const store = seedChats()
    store.chats = [
      ...store.chats,
      {
        chat_id: 'asker', project_id: 'personal-project', title: 'Asks a question',
        pending_question: JSON.stringify({ questions: [{ question: 'Internal?' }] }),
        created_at: timestamp(30), last_activity_at: timestamp(30), last_read_at: timestamp(30), archived: false, local: true,
      },
    ] as unknown as typeof store.chats
    const { default: HomeRecentChats } = await import('../HomeRecentChats.vue')
    const wrapper = mount(HomeRecentChats, { attachTo: document.body })
    await nextTick()

    const personalLane = wrapper.find('[data-lane-key="personal"]')
    const summary = personalLane.find('.home-lane-status-text').text()
    const rows = personalLane.findAll('.home-tier--needsYou .home-chat-item').length
    const claimed = Number(/(\d+)\s+chat(?:s)?\s+need/.exec(summary)?.[1] ?? 0)
    expect(claimed).toBe(rows)
    wrapper.unmount()
  })
})

// The header is a single line: the workspace name and its status phrase sit
// together in the topline row, with "+ new" at the far end. The old face-and-
// sentence status row is gone.
describe('the lane header line', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.restoreAllMocks()
    if (!Element.prototype.scrollIntoView) Element.prototype.scrollIntoView = () => {}
  })

  it('keeps the selected lane header for screen readers only', async () => {
    const wrapper = await mountHome()
    const lane = wrapper.find('[data-lane-key="personal"]')
    const header = lane.get('.home-lane-header')
    expect(header.classes()).toContain('home-lane-header--quiet')
    expect(header.find('.home-lane-name').exists()).toBe(false)
    expect(header.find('.home-lane-shortcut').exists()).toBe(false)
    expect(header.get('.home-lane-status-text').attributes('aria-live')).toBe('polite')
    wrapper.unmount()
  })

  it('speaks the needs and working state in the status phrase', async () => {
    const wrapper = await mountHome()
    const personal = wrapper.find('[data-lane-key="personal"]')
    // Seeded: one needs-you chat in personal, no agents working.
    expect(personal.find('.home-lane-status-text').text())
      .toBe('1 chat needs your attention. no agents working.')
    wrapper.unmount()
  })

  it('counts unread replies in the status phrase', async () => {
    const store = seedChats()
    store.chats = [
      ...store.chats,
      {
        chat_id: 'unread', project_id: 'personal-project', title: 'Unread reply',
        created_at: timestamp(30), last_activity_at: timestamp(30), last_read_at: timestamp(90),
        archived: false, local: true,
      },
    ] as unknown as typeof store.chats
    const { default: HomeRecentChats } = await import('../HomeRecentChats.vue')
    const wrapper = mount(HomeRecentChats, { attachTo: document.body })
    await nextTick()

    const lane = wrapper.find('[data-lane-key="personal"]')
    expect(lane.find('.home-lane-status-text').text())
      .toBe('2 chats need your attention. no agents working.')
    wrapper.unmount()
  })

  it('counts memory work in flight in the status phrase', async () => {
    const store = seedChats()
    store.chats = [
      ...store.chats,
      {
        chat_id: 'tidy', project_id: 'personal-project', title: 'Archived chat',
        created_at: timestamp(300), last_activity_at: timestamp(300), last_read_at: timestamp(300),
        archived: true, local: true, archive_path: 'archive/tidy.md',
      },
    ] as unknown as typeof store.chats
    store.archivingChats = { tidy: true }
    const { default: HomeRecentChats } = await import('../HomeRecentChats.vue')
    const wrapper = mount(HomeRecentChats, { attachTo: document.body })
    await nextTick()

    const lane = wrapper.find('[data-lane-key="personal"]')
    expect(lane.find('.home-lane-status-text').text())
      .toBe('1 chat needs your attention. no agents working. 1 updating memory.')
    wrapper.unmount()
  })
})

describe('HomeRecentChats new-chat entry', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.restoreAllMocks()
    if (!Element.prototype.scrollIntoView) Element.prototype.scrollIntoView = () => {}
  })

  it('leaves new work to the composer on the selected lane', async () => {
    const wrapper = await mountHome()
    expect(wrapper.find('[data-lane-key="personal"] .home-lane-new').exists()).toBe(false)
    wrapper.unmount()
  })

  it('keeps a New action on a rescue lane and routes it to the shared picker', async () => {
    const store = seedChats()
    store.projects = [
      ...store.projects,
      { project_id: 'stale-general', name: 'General', workspace: 'renamed-away' },
    ] as unknown as typeof store.projects
    store.chats = store.chats.map(chat =>
      chat.chat_id === 'quiet' ? { ...chat, project_id: 'stale-general' } : chat,
    ) as unknown as typeof store.chats
    const { default: HomeRecentChats } = await import('../HomeRecentChats.vue')
    const wrapper = mount(HomeRecentChats, { attachTo: document.body })
    await nextTick()

    const rescue = wrapper.get('[data-lane-key="renamed-away"]')
    expect(rescue.get('.home-lane-name').text()).toBe('renamed away')
    const button = rescue.get('.home-lane-new')
    expect(button.attributes('aria-haspopup')).toBe('dialog')
    await button.trigger('click')
    expect(wrapper.emitted('choose-new-chat')?.[0]).toEqual(['renamed-away'])
    wrapper.unmount()
  })
})
