// @vitest-environment jsdom

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'

function seedChats(count: number) {
  const store = useProjectStore()
  store.projects = [{
    project_id: 'project-1',
    name: 'Project',
    workspace: 'personal',
  }] as unknown as typeof store.projects
  store.chats = Array.from({ length: count }, (_, i) => ({
    chat_id: `chat-${i + 1}`,
    project_id: 'project-1',
    title: `Chat ${i + 1}`,
    created_at: `2026-08-0${i + 1}T00:00:00Z`,
    last_activity_at: `2026-08-0${i + 1}T00:00:00Z`,
    archived: false,
    local: true,
  })) as unknown as typeof store.chats
  store.bootstrapped = true
  return store
}

async function mountGrid(count: number) {
  seedChats(count)
  const taskStore = useTaskStore()
  taskStore.loops = [] as unknown as typeof taskStore.loops
  const { default: HomeRecentChats } = await import('../HomeRecentChats.vue')
  const wrapper = mount(HomeRecentChats, { attachTo: document.body })
  await nextTick()
  return wrapper
}

describe('HomeRecentChats arrow navigation', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.restoreAllMocks()
    // jsdom does not implement scrollIntoView; onArrow calls it right after
    // focus(). Without this every navigation throws here but not in a browser.
    if (!Element.prototype.scrollIntoView) {
      Element.prototype.scrollIntoView = () => {}
    }
  })

  it('renders one card per non-archived chat', async () => {
    const wrapper = await mountGrid(4)
    expect(wrapper.findAll('.home-recent-card')).toHaveLength(4)
  })

  it('focuses the first card when nothing is focused yet', async () => {
    const wrapper = await mountGrid(4)
    const consumed = (wrapper.vm as unknown as { onArrow: (k: string) => boolean })
      .onArrow('ArrowRight')

    expect(consumed).toBe(true)
    const cards = wrapper.findAll('.home-recent-card')
    expect(document.activeElement).toBe(cards[0].element)
  })

  it('moves focus to the next card on a second ArrowRight', async () => {
    const wrapper = await mountGrid(4)
    const vm = wrapper.vm as unknown as { onArrow: (k: string) => boolean }
    const cards = wrapper.findAll('.home-recent-card')

    vm.onArrow('ArrowRight')
    expect(document.activeElement).toBe(cards[0].element)

    // This is the motion the bug report says does not happen.
    vm.onArrow('ArrowRight')
    expect(document.activeElement).toBe(cards[1].element)

    vm.onArrow('ArrowLeft')
    expect(document.activeElement).toBe(cards[0].element)
  })

  it('consumes the key at the edges instead of letting the page scroll', async () => {
    const wrapper = await mountGrid(2)
    const vm = wrapper.vm as unknown as { onArrow: (k: string) => boolean }

    vm.onArrow('ArrowRight')
    expect(vm.onArrow('ArrowLeft')).toBe(true) // already at index 0
  })

  it('reports the key unconsumed when there are no chats to navigate', async () => {
    const wrapper = await mountGrid(0)
    const vm = wrapper.vm as unknown as { onArrow: (k: string) => boolean }
    expect(vm.onArrow('ArrowRight')).toBe(false)
  })
})
