// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'

function timestamp(secondsAgo: number): string {
  return new Date(Date.now() - secondsAgo * 1000).toISOString()
}

function seedChats(includeChats = true) {
  const store = useProjectStore()
  store.workspaces = [
    { name: 'personal', vault_root: '', default_provider: 'claude', default_model: '', gws_profile: '', model_bucket: '', color: 'pink' },
    { name: 'work', vault_root: '', default_provider: 'claude', default_model: '', gws_profile: '', model_bucket: '', color: 'cyan' },
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

  it('renders one lane per workspace in workspaceOptions order', async () => {
    const wrapper = await mountHome()
    expect(wrapper.findAll('.home-lane-name').map(node => node.text())).toEqual(['personal', 'work'])
    expect(wrapper.findAll('.home-lane')).toHaveLength(2)
  })

  it('assigns chats to one priority tier and shows the pending question', async () => {
    const wrapper = await mountHome()
    expect(wrapper.find('.home-tier--needsYou .home-chat-title').text()).toBe('Needs an answer')
    expect(wrapper.find('.home-chat-question').text()).toContain('Which launch date')
    expect(wrapper.find('.home-tier--working .home-chat-title').text()).toBe('Background work')
    expect(wrapper.find('.home-tier--quiet .home-chat-title').text()).toBe('A quiet chat')
    // Older chats are listed in quiet now, not split behind a disclosure, so
    // every seeded chat renders as a row.
    expect(wrapper.find('.home-lane-older-toggle').exists()).toBe(false)
    expect(wrapper.findAll('.home-chat-item')).toHaveLength(4)
  })

  it('lists older chats inline with quiet instead of behind a disclosure', async () => {
    const wrapper = await mountHome()
    const quietTitles = wrapper.findAll('.home-tier--quiet .home-chat-title').map(n => n.text())
    expect(quietTitles).toContain('A quiet chat')
    expect(quietTitles).toContain('An older chat')
    expect(wrapper.find('.home-tier--older').exists()).toBe(false)
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
    expect(labels).toContain('working')
    expect(labels.some(l => l === l.toUpperCase() && /[A-Z]/.test(l))).toBe(false)
  })

  it('keeps vertical motion within a lane and horizontal motion across lanes', async () => {
    const wrapper = await mountHome()
    const vm = wrapper.vm as unknown as { onArrow: (key: string) => boolean }
    const cards = wrapper.findAll('.home-chat-item')

    expect(vm.onArrow('ArrowDown')).toBe(true)
    expect(document.activeElement).toBe(cards[0].element)
    expect(vm.onArrow('ArrowDown')).toBe(true)
    expect(document.activeElement).toBe(cards[1].element)
    expect(vm.onArrow('ArrowUp')).toBe(true)
    expect(document.activeElement).toBe(cards[0].element)
    expect(vm.onArrow('ArrowRight')).toBe(true)
    expect(document.activeElement).toBe(wrapper.find('.home-tier--working .home-chat-item').element)
    expect(vm.onArrow('ArrowLeft')).toBe(true)
    expect(document.activeElement).toBe(cards[0].element)
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
