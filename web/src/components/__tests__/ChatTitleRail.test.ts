// @vitest-environment jsdom

import { readFileSync } from 'node:fs'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { mount } from '@vue/test-utils'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'
import { RAIL_SETTLE_MS, RAIL_SLIDE_MS, railItemLabel, type ChatRailItem } from '../../lib/chatRail'
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
  })

  it('asks to rename when the current title is double-clicked', () => {
    seed(['a', 'b'])
    const wrapper = mount(ChatTitleRail, { props: { items: [item('a'), item('b')], currentId: 'b' } })

    void wrapper.findAll('.title-rail-item')[1].trigger('dblclick')

    expect(wrapper.emitted('rename')).toHaveLength(1)
  })

  // The neighbours are reached by swiping or stepping, never by buttons or by clicking a peeking title.
  it('has no arrow buttons', () => {
    seed(['a', 'b', 'c'])
    const wrapper = mount(ChatTitleRail, { props: { items: [item('a'), item('b'), item('c')], currentId: 'b' } })

    expect(wrapper.find('button[aria-label="Previous chat"]').exists()).toBe(false)
    expect(wrapper.find('button[aria-label="Next chat"]').exists()).toBe(false)
  })

  it('draws one track segment per chat, marking each chat\'s state by shape', () => {
    seed(['a', 'b', 'c', 'd'])
    const wrapper = mount(ChatTitleRail, {
      props: {
        items: [item('a', { tier: 'needsYou' }), item('b'), item('c', { tier: 'working' }), item('d', { tier: 'unread' })],
        currentId: 'b',
      },
    })

    const track = wrapper.get('.title-rail-track')
    expect(track.attributes('aria-hidden')).toBe('true')
    const segs = track.findAll('.title-rail-seg')
    expect(segs).toHaveLength(4)
    expect(segs[0].classes()).toContain('title-rail-seg--needs')
    expect(segs[1].classes()).toEqual(['title-rail-seg'])
    expect(segs[2].classes()).toContain('title-rail-seg--working')
    expect(segs[3].classes()).toContain('title-rail-seg--unread')
    // The bright segment sits on the current chat.
    expect(track.attributes('style')).toContain('--n: 4')
    expect(track.attributes('style')).toContain('--pos: 1')
    // Nothing but the title and project sits on the text lines.
    expect(wrapper.find('.title-rail-dot').exists()).toBe(false)
  })

  it('moves the bright segment with the strip mid-swipe', () => {
    seed(['a', 'b', 'c'])
    const wrapper = mount(ChatTitleRail, {
      props: { items: [item('a'), item('b'), item('c')], currentId: 'b' },
      attachTo: document.body,
    })
    const strip = geometry(wrapper)
    strip.scrollLeft = 140
    strip.dispatchEvent(new Event('scroll'))
    return wrapper.vm.$nextTick().then(() => {
      expect(wrapper.get('.title-rail-track').attributes('style')).toContain('--pos: 1.4')
      wrapper.unmount()
    })
  })

  function pointer(type: string, x: number, y: number, pointerType = 'touch', pointerId = 1): Event {
    const e = new MouseEvent(type, { clientX: x, clientY: y, button: 0, bubbles: true, cancelable: true })
    Object.defineProperty(e, 'pointerId', { value: pointerId })
    Object.defineProperty(e, 'pointerType', { value: pointerType })
    return e
  }

  it('reports a downward drag on the title as a pull, and its end with distance and speed', () => {
    seed(['a', 'b'])
    const wrapper = mount(ChatTitleRail, { props: { items: [item('a'), item('b')], currentId: 'a' }, attachTo: document.body })
    const root = wrapper.get('.title-rail').element as HTMLElement
    root.setPointerCapture = () => {}
    root.dispatchEvent(pointer('pointerdown', 100, 10))
    root.dispatchEvent(pointer('pointermove', 102, 40))
    root.dispatchEvent(pointer('pointermove', 103, 140))
    root.dispatchEvent(pointer('pointerup', 103, 140))

    expect(wrapper.emitted('pull')).toEqual([[30], [130]])
    const [[dy, velocity]] = wrapper.emitted('pullEnd') as [number, number][]
    expect(dy).toBe(130)
    expect(velocity).toBeGreaterThan(0)
    expect(wrapper.emitted('select')).toBeUndefined()
    wrapper.unmount()
  })

  it('springs back instead of committing a canceled downward drag beyond the close threshold', () => {
    seed(['a', 'b'])
    const wrapper = mount(ChatTitleRail, { props: { items: [item('a'), item('b')], currentId: 'a' }, attachTo: document.body })
    const root = wrapper.get('.title-rail').element as HTMLElement
    root.setPointerCapture = () => {}
    root.dispatchEvent(pointer('pointerdown', 100, 10))
    root.dispatchEvent(pointer('pointermove', 100, 180))
    root.dispatchEvent(pointer('pointercancel', 100, 180))
    expect(wrapper.emitted('pullEnd')).toEqual([[0, 0]])
    expect(wrapper.emitted('select')).toBeUndefined()
    wrapper.unmount()
  })

  it('restores the current title instead of selecting after a canceled mouse drag beyond the switch threshold', () => {
    vi.useFakeTimers()
    seed(['a', 'b', 'c'])
    const wrapper = mount(ChatTitleRail, { props: { items: [item('a'), item('b'), item('c')], currentId: 'b' }, attachTo: document.body })
    const root = wrapper.get('.title-rail').element as HTMLElement
    const strip = geometry(wrapper)
    const scrollTo = vi.fn()
    strip.scrollTo = scrollTo as unknown as typeof strip.scrollTo
    root.setPointerCapture = () => {}
    root.dispatchEvent(pointer('pointerdown', 100, 10, 'mouse'))
    root.dispatchEvent(pointer('pointermove', 30, 10, 'mouse'))
    root.dispatchEvent(pointer('pointercancel', 30, 10, 'mouse'))
    vi.advanceTimersByTime(RAIL_SLIDE_MS + RAIL_SETTLE_MS)
    expect(wrapper.emitted('select')).toBeUndefined()
    expect(strip.style.scrollSnapType).not.toBe('none')
    expect(scrollTo).toHaveBeenCalled()
    wrapper.unmount()
  })

  it('ignores a second pointer until the first drag ends', () => {
    seed(['a', 'b'])
    const wrapper = mount(ChatTitleRail, { props: { items: [item('a'), item('b')], currentId: 'a' }, attachTo: document.body })
    const root = wrapper.get('.title-rail').element as HTMLElement
    root.setPointerCapture = () => {}
    root.dispatchEvent(pointer('pointerdown', 100, 10, 'touch', 1))
    root.dispatchEvent(pointer('pointermove', 100, 50, 'touch', 1))
    root.dispatchEvent(pointer('pointerdown', 200, 10, 'touch', 2))
    root.dispatchEvent(pointer('pointermove', 200, 200, 'touch', 2))
    root.dispatchEvent(pointer('pointerup', 200, 200, 'touch', 2))
    root.dispatchEvent(pointer('pointerup', 100, 50, 'touch', 1))
    expect(wrapper.emitted('pull')).toEqual([[40]])
    expect((wrapper.emitted('pullEnd') as [number, number][])[0][0]).toBe(40)
    expect(wrapper.emitted('pullEnd')).toHaveLength(1)
    wrapper.unmount()
  })

  it('a sideways touch drag past a quarter of the width slides to the neighbour', () => {
    vi.useFakeTimers()
    seed(['a', 'b', 'c'])
    const wrapper = mount(ChatTitleRail, { props: { items: [item('a'), item('b'), item('c')], currentId: 'b' }, attachTo: document.body })
    const root = wrapper.get('.title-rail').element as HTMLElement
    const strip = geometry(wrapper)
    strip.scrollTo = vi.fn() as unknown as typeof strip.scrollTo
    root.setPointerCapture = () => {}
    root.dispatchEvent(pointer('pointerdown', 200, 10))
    root.dispatchEvent(pointer('pointermove', 120, 12))
    root.dispatchEvent(pointer('pointermove', 60, 12))
    root.dispatchEvent(pointer('pointerup', 60, 12))
    vi.advanceTimersByTime(RAIL_SLIDE_MS)
    expect(wrapper.emitted('select')).toEqual([['c']])
    wrapper.unmount()
  })

  it('a short sideways touch drag springs back without selecting', () => {
    vi.useFakeTimers()
    seed(['a', 'b', 'c'])
    const wrapper = mount(ChatTitleRail, { props: { items: [item('a'), item('b'), item('c')], currentId: 'b' }, attachTo: document.body })
    const root = wrapper.get('.title-rail').element as HTMLElement
    const strip = geometry(wrapper)
    const scrollTo = vi.fn()
    strip.scrollTo = scrollTo as unknown as typeof strip.scrollTo
    root.setPointerCapture = () => {}
    root.dispatchEvent(pointer('pointerdown', 100, 10))
    root.dispatchEvent(pointer('pointermove', 85, 10))
    root.dispatchEvent(pointer('pointerup', 85, 10))
    vi.advanceTimersByTime(RAIL_SLIDE_MS + RAIL_SETTLE_MS)
    expect(wrapper.emitted('select')).toBeUndefined()
    expect(scrollTo).toHaveBeenCalled()
    wrapper.unmount()
  })

  it('a canceled sideways touch drag restores the title', () => {
    vi.useFakeTimers()
    seed(['a', 'b', 'c'])
    const wrapper = mount(ChatTitleRail, { props: { items: [item('a'), item('b'), item('c')], currentId: 'b' }, attachTo: document.body })
    const root = wrapper.get('.title-rail').element as HTMLElement
    const strip = geometry(wrapper)
    const scrollTo = vi.fn()
    strip.scrollTo = scrollTo as unknown as typeof strip.scrollTo
    root.setPointerCapture = () => {}
    root.dispatchEvent(pointer('pointerdown', 200, 10))
    root.dispatchEvent(pointer('pointermove', 130, 10))
    root.dispatchEvent(pointer('pointercancel', 130, 10))
    vi.advanceTimersByTime(RAIL_SLIDE_MS + RAIL_SETTLE_MS)
    expect(wrapper.emitted('select')).toBeUndefined()
    expect(scrollTo).toHaveBeenCalledWith({ left: 100, behavior: 'smooth' })
    wrapper.unmount()
  })

  it('ignores an upward drag', () => {
    seed(['a', 'b'])
    const wrapper = mount(ChatTitleRail, { props: { items: [item('a'), item('b')], currentId: 'a' }, attachTo: document.body })
    const root = wrapper.get('.title-rail').element as HTMLElement
    root.setPointerCapture = () => {}
    root.dispatchEvent(pointer('pointerdown', 100, 100))
    root.dispatchEvent(pointer('pointermove', 100, 40))
    root.dispatchEvent(pointer('pointerup', 100, 40))

    expect(wrapper.emitted('pull')).toBeUndefined()
    expect(wrapper.emitted('pullEnd')).toBeUndefined()
    wrapper.unmount()
  })

  it('a horizontal wheel scrolls the strip and settles on the neighbour', () => {
    vi.useFakeTimers()
    seed(['a', 'b', 'c'])
    const wrapper = mount(ChatTitleRail, {
      props: { items: [item('a'), item('b'), item('c')], currentId: 'b' },
      attachTo: document.body,
    })
    const strip = geometry(wrapper)
    strip.scrollTo = vi.fn() as unknown as typeof strip.scrollTo
    strip.scrollLeft = 100

    const wheel = new WheelEvent('wheel', { deltaX: 120, deltaY: 0, cancelable: true, bubbles: true })
    strip.dispatchEvent(wheel)
    expect(wheel.defaultPrevented).toBe(true)
    expect(strip.scrollLeft).toBe(220)
    vi.advanceTimersByTime(RAIL_SETTLE_MS)
    expect(wrapper.emitted('select')).toEqual([['c']])

    // A vertical wheel is the page scrolling, so it is left alone.
    const vertical = new WheelEvent('wheel', { deltaY: 50, cancelable: true, bubbles: true })
    strip.dispatchEvent(vertical)
    expect(vertical.defaultPrevented).toBe(false)
    wrapper.unmount()
  })

  it('reports how far the strip leans from the current chat mid-swipe', () => {
    seed(['a', 'b', 'c'])
    const wrapper = mount(ChatTitleRail, { props: { items: [item('a'), item('b'), item('c')], currentId: 'b' }, attachTo: document.body })
    const strip = geometry(wrapper)
    strip.scrollLeft = 130
    strip.dispatchEvent(new Event('scroll'))
    const leans = wrapper.emitted('lean') as [number][]
    expect(leans.at(-1)![0]).toBeCloseTo(0.3)
    wrapper.unmount()
  })

  // The arrow keys step the chat, sliding the neighbour in first like a swipe.
  it('steps to a neighbour with the arrow keys from the current item, after the slide', async () => {
    vi.useFakeTimers()
    seed(['a', 'b', 'c'])
    const wrapper = mount(ChatTitleRail, {
      props: { items: [item('a'), item('b'), item('c')], currentId: 'b' },
    })
    const current = wrapper.get('.title-rail-item[aria-current="page"]')

    await current.trigger('keydown', { key: 'ArrowLeft' })
    expect(wrapper.emitted('select')).toBeUndefined()
    // A second step while the first slides is ignored.
    await current.trigger('keydown', { key: 'ArrowRight' })
    vi.advanceTimersByTime(RAIL_SLIDE_MS)
    expect(wrapper.emitted('select')).toEqual([['a']])

    await current.trigger('keydown', { key: 'ArrowRight' })
    vi.advanceTimersByTime(RAIL_SLIDE_MS)
    expect(wrapper.emitted('select')).toEqual([['a'], ['c']])
  })

  it('slides the strip to the neighbour before switching', () => {
    vi.useFakeTimers()
    seed(['a', 'b', 'c'])
    const wrapper = mount(ChatTitleRail, {
      props: { items: [item('a'), item('b'), item('c')], currentId: 'b' },
      attachTo: document.body,
    })
    const strip = geometry(wrapper)
    const scrollTo = vi.fn()
    strip.scrollTo = scrollTo as unknown as typeof strip.scrollTo

    const exposed = wrapper.vm as unknown as { slide: (d: -1 | 1) => boolean }
    expect(exposed.slide(1)).toBe(true)

    expect(scrollTo).toHaveBeenCalledWith({ left: 200, behavior: 'smooth' })
    // The slide's own scroll events never count as a swipe.
    strip.dispatchEvent(new Event('scroll'))
    vi.advanceTimersByTime(RAIL_SLIDE_MS)
    expect(wrapper.emitted('select')).toEqual([['c']])
    wrapper.unmount()
  })

  it('switches at once under reduced motion, and reports when there is no neighbour', () => {
    seed(['a', 'b'])
    const matchMedia = vi.fn(() => ({ matches: true }))
    vi.stubGlobal('matchMedia', matchMedia)
    try {
      const wrapper = mount(ChatTitleRail, { props: { items: [item('a'), item('b')], currentId: 'a' } })
      const exposed = wrapper.vm as unknown as { slide: (d: -1 | 1) => boolean }
      expect(exposed.slide(-1)).toBe(false)
      expect(exposed.slide(1)).toBe(true)
      expect(wrapper.emitted('select')).toEqual([['b']])
    } finally {
      vi.unstubAllGlobals()
    }
  })

  // A browser chord such as Alt+ArrowLeft is Back: the rail must leave it alone.
  it('ignores an arrow key pressed with a modifier', () => {
    seed(['a', 'b', 'c'])
    const wrapper = mount(ChatTitleRail, {
      props: { items: [item('a'), item('b'), item('c')], currentId: 'b' },
    })
    const current = wrapper.get('.title-rail-item[aria-current="page"]').element as HTMLElement
    const event = new KeyboardEvent('keydown', { key: 'ArrowRight', altKey: true, bubbles: true, cancelable: true })

    current.dispatchEvent(event)

    expect(wrapper.emitted('select')).toBeUndefined()
    expect(event.defaultPrevented).toBe(false)
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

  // iOS 26+ gives a native scroller near the top edge the soft scroll-edge blur over the title.
  it('the strip is never a native scroller', () => {
    const source = readFileSync(join(__dirname, '..', 'ChatTitleRail.vue'), 'utf8')
    const rule = source.match(/\.title-rail-strip\s*\{([^}]*)\}/)?.[1] ?? ''
    expect(rule).toContain('overflow: hidden')
    expect(rule).not.toMatch(/overflow(-x)?:\s*(auto|scroll)/)
  })

})
