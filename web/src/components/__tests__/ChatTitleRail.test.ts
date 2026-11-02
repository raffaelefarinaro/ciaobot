// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'
import { RAIL_SETTLE_MS, railItemLabel, type ChatRailItem } from '../../lib/chatRail'
import ChatTitleRail from '../ChatTitleRail.vue'

function item(chatId: string, overrides: Partial<ChatRailItem> = {}): ChatRailItem {
  return {
    chatId,
    title: `title ${chatId}`,
    project: 'General',
    tier: 'quiet',
    titlePending: false,
    ...overrides,
  }
}

function seed(ids: string[]) {
  const store = useProjectStore()
  store.projects = [{ project_id: 'project-1', name: 'General', workspace: 'personal' }] as unknown as typeof store.projects
  store.chats = ids.map(chatId => ({
    chat_id: chatId,
    project_id: 'project-1',
    title: `title ${chatId}`,
    archived: false,
    local: true,
  })) as unknown as typeof store.chats
  store.projectStreaming = {}
  store.backgroundAgents = {}
  const taskStore = useTaskStore()
  taskStore.schedules = [] as unknown as typeof taskStore.schedules
  return store
}

describe('ChatTitleRail', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('renders every item with the current one marked', () => {
    seed(['a', 'b', 'c'])
    const wrapper = mount(ChatTitleRail, {
      props: { items: [item('a'), item('b'), item('c')], currentId: 'b' },
    })

    const buttons = wrapper.findAll('.title-rail-item')
    expect(buttons).toHaveLength(3)

    const current = wrapper.findAll('.title-rail-item[aria-current="page"]')
    expect(current).toHaveLength(1)
    expect(current[0].attributes('aria-label')).toContain('title b')
    expect(buttons[1].attributes('aria-current')).toBe('page')
    expect(buttons[1].attributes('tabindex')).toBe('0')
    expect(buttons[0].attributes('tabindex')).toBe('-1')
    expect(buttons[2].attributes('tabindex')).toBe('-1')

    expect(wrapper.find('.title-rail').attributes('aria-label')).toBe('Chats in this workspace, 2 of 3')
    expect(wrapper.find('.title-rail-item[aria-current] .chat-title').text()).toBe('title b')
    expect(wrapper.find('.title-rail-item[aria-current] .title-rail-project').text()).toBe('General')
  })

  it('labels items for assistive tech', () => {
    seed(['a', 'b'])
    const a = item('a', { tier: 'needsYou' })
    const wrapper = mount(ChatTitleRail, { props: { items: [a, item('b')], currentId: 'b' } })

    const label = wrapper.findAll('.title-rail-item')[0].attributes('aria-label')
    expect(label).toBe(railItemLabel(a))
    expect(label).toContain('needs you')
    expect(wrapper.find('.title-rail-signals').attributes('aria-hidden')).toBe('true')
  })

  it('emits select when a neighbour is clicked, and nothing for the current item', () => {
    seed(['a', 'b', 'c'])
    const wrapper = mount(ChatTitleRail, {
      props: { items: [item('a'), item('b'), item('c')], currentId: 'b' },
    })
    const buttons = wrapper.findAll('.title-rail-item')

    void buttons[2].trigger('click')
    void buttons[1].trigger('click')

    expect(wrapper.emitted('select')).toEqual([['c']])
  })

  it('asks to rename when the current title is double-clicked', () => {
    seed(['a', 'b'])
    const wrapper = mount(ChatTitleRail, { props: { items: [item('a'), item('b')], currentId: 'b' } })

    void wrapper.findAll('.title-rail-item')[1].trigger('dblclick')

    expect(wrapper.emitted('rename')).toHaveLength(1)
  })

  it('arrow buttons step and disable at the ends', async () => {
    seed(['a', 'b', 'c'])
    const items = [item('a'), item('b'), item('c')]

    const first = mount(ChatTitleRail, { props: { items, currentId: 'a' } })
    expect(first.find('button[aria-label="Previous chat"]').attributes('disabled')).toBeDefined()
    await first.find('button[aria-label="Next chat"]').trigger('click')
    expect(first.emitted('select')).toEqual([['b']])

    const last = mount(ChatTitleRail, { props: { items, currentId: 'c' } })
    expect(last.find('button[aria-label="Next chat"]').attributes('disabled')).toBeDefined()
  })

  it('marks needs-you dots in the accent and caps the row', () => {
    seed(['a', 'b', 'c'])
    const wrapper = mount(ChatTitleRail, {
      props: { items: [item('a', { tier: 'needsYou' }), item('b'), item('c')], currentId: 'b' },
    })

    const dots = wrapper.findAll('.title-rail-dot')
    expect(dots[0].classes()).toContain('is-needs')
    expect(dots[1].classes()).toContain('is-current')
    expect(wrapper.find('.title-rail-dots').attributes('aria-hidden')).toBe('true')

    const many = Array.from({ length: 13 }, (_, i) => item(`chat-${i}`))
    const capped = mount(ChatTitleRail, { props: { items: many, currentId: 'chat-0' } })
    expect(capped.findAll('.title-rail-dot')).toHaveLength(0)
    expect(capped.find('.title-rail-count').text()).toBe('1 / 13')
  })

  it('gives the count room only above the dots cap', () => {
    seed(['chat-0'])
    const many = (n: number) => Array.from({ length: n }, (_, i) => item(`chat-${i}`))

    expect(mount(ChatTitleRail, { props: { items: many(12), currentId: 'chat-0' } }).classes()).not.toContain('title-rail--count')
    expect(mount(ChatTitleRail, { props: { items: many(13), currentId: 'chat-0' } }).classes()).toContain('title-rail--count')
  })

  // Item 7: with no arrow buttons (phones), the arrow keys still step the chat.
  it('steps to a neighbour with the arrow keys from the current item', async () => {
    seed(['a', 'b', 'c'])
    const wrapper = mount(ChatTitleRail, {
      props: { items: [item('a'), item('b'), item('c')], currentId: 'b' },
    })
    const current = wrapper.get('.title-rail-item[aria-current="page"]')

    await current.trigger('keydown', { key: 'ArrowLeft' })
    await current.trigger('keydown', { key: 'ArrowRight' })

    expect(wrapper.emitted('select')).toEqual([['a'], ['c']])
  })

  it('does nothing for an arrow key at either end of the rail', async () => {
    seed(['a', 'b'])
    const first = mount(ChatTitleRail, { props: { items: [item('a'), item('b')], currentId: 'a' } })
    await first.get('.title-rail-item[aria-current="page"]').trigger('keydown', { key: 'ArrowLeft' })
    expect(first.emitted('select')).toBeUndefined()

    const last = mount(ChatTitleRail, { props: { items: [item('a'), item('b')], currentId: 'b' } })
    await last.get('.title-rail-item[aria-current="page"]').trigger('keydown', { key: 'ArrowRight' })
    expect(last.emitted('select')).toBeUndefined()
  })

  function geometry(wrapper: ReturnType<typeof mount>) {
    const strip = wrapper.find('.title-rail-strip').element as HTMLElement
    const els = wrapper.findAll('.title-rail-item').map(w => w.element as HTMLElement)
    els.forEach((el, i) => {
      Object.defineProperty(el, 'offsetLeft', { configurable: true, value: i * 100 })
      Object.defineProperty(el, 'offsetWidth', { configurable: true, value: 100 })
    })
    Object.defineProperty(strip, 'clientWidth', { configurable: true, value: 100 })
    return strip
  }

  it('settles on a neighbour after a gesture', () => {
    vi.useFakeTimers()
    seed(['a', 'b', 'c'])
    const wrapper = mount(ChatTitleRail, {
      props: { items: [item('a'), item('b'), item('c')], currentId: 'b' },
      attachTo: document.body,
    })
    const strip = geometry(wrapper)

    strip.scrollLeft = 200
    strip.dispatchEvent(new WheelEvent('wheel', { deltaX: 50 }))
    strip.dispatchEvent(new Event('scroll'))
    vi.advanceTimersByTime(RAIL_SETTLE_MS)

    expect(wrapper.emitted('select')).toEqual([['c']])
    wrapper.unmount()
  })

  it('does not switch on a scroll it did not see a gesture for', () => {
    vi.useFakeTimers()
    seed(['a', 'b', 'c'])
    const wrapper = mount(ChatTitleRail, {
      props: { items: [item('a'), item('b'), item('c')], currentId: 'b' },
      attachTo: document.body,
    })
    const strip = geometry(wrapper)

    strip.scrollLeft = 200
    strip.dispatchEvent(new Event('scroll'))
    vi.advanceTimersByTime(RAIL_SETTLE_MS)

    expect(wrapper.emitted('select')).toBeUndefined()
    wrapper.unmount()
  })

  it('does not switch after a vertical wheel, which is page scrolling', () => {
    vi.useFakeTimers()
    seed(['a', 'b', 'c'])
    const wrapper = mount(ChatTitleRail, {
      props: { items: [item('a'), item('b'), item('c')], currentId: 'b' },
      attachTo: document.body,
    })
    const strip = geometry(wrapper)

    strip.dispatchEvent(new WheelEvent('wheel', { deltaY: 40 }))
    strip.scrollLeft = 200
    strip.dispatchEvent(new Event('scroll'))
    vi.advanceTimersByTime(RAIL_SETTLE_MS)

    expect(wrapper.emitted('select')).toBeUndefined()
    wrapper.unmount()
  })

  it('waits for the finger to lift', () => {
    vi.useFakeTimers()
    seed(['a', 'b', 'c'])
    const wrapper = mount(ChatTitleRail, {
      props: { items: [item('a'), item('b'), item('c')], currentId: 'b' },
      attachTo: document.body,
    })
    const strip = geometry(wrapper)

    strip.scrollLeft = 200
    strip.dispatchEvent(new Event('touchstart'))
    strip.dispatchEvent(new Event('scroll'))
    vi.advanceTimersByTime(RAIL_SETTLE_MS)
    expect(wrapper.emitted('select')).toBeUndefined()

    strip.dispatchEvent(new Event('touchend'))
    vi.advanceTimersByTime(RAIL_SETTLE_MS)
    expect(wrapper.emitted('select')).toEqual([['c']])
    wrapper.unmount()
  })
})
