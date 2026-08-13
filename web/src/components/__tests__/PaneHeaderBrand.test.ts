// @vitest-environment jsdom

/**
 * The pane header carries the brand mark and the page tag.
 *
 * The brand was moved out of the sidebar into the middle of this header, so what
 * these tests protect is (a) the reload behaviour survived the move intact, and
 * (b) the structure that makes the mark genuinely centred: the mark lives in its
 * own grid column, a sibling of the title, so no title length can move it.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h, nextTick } from 'vue'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'
import BrandMark from '../BrandMark.vue'
import PaneHeader from '../PaneHeader.vue'

// The bell reaches for stores and a teleport target; neither is what is under
// test here, and PaneHeader imports it directly so `stubs` cannot reach it.
vi.mock('../NotificationBell.vue', () => ({
  default: defineComponent({ name: 'NotificationBell', setup: () => () => h('div', { class: 'bell-stub' }) }),
}))

const LONG_TITLE = 'A chat title long enough to need every pixel of the header and then a great many more '.repeat(3)

describe('BrandMark', () => {
  const originalLocation = Object.getOwnPropertyDescriptor(window, 'location')
  let replace: ReturnType<typeof vi.fn>

  beforeEach(() => {
    replace = vi.fn()
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { href: 'http://localhost/chat/abc', replace },
    })
    // Reduced motion, so the pixel-jitter interval never starts and the test
    // does not depend on a timer.
    window.matchMedia = vi.fn().mockReturnValue({ matches: true }) as unknown as typeof window.matchMedia
  })

  afterEach(() => {
    if (originalLocation) Object.defineProperty(window, 'location', originalLocation)
    vi.restoreAllMocks()
  })

  it('renders the wordmark', () => {
    const wrapper = mount(BrandMark)
    expect(wrapper.find('.brand-label').text()).toBe('ciaobot')
    expect(wrapper.attributes('title')).toBe('Click to reload the latest app build')
    expect(wrapper.attributes('aria-busy')).toBe('false')
    wrapper.unmount()
  })

  it('reloads the latest build with a cache-busting query when clicked', async () => {
    const wrapper = mount(BrandMark)
    await wrapper.trigger('click')
    await flushPromises()

    expect(replace).toHaveBeenCalledTimes(1)
    const target = new URL(replace.mock.calls[0][0] as string)
    expect(target.pathname).toBe('/chat/abc')
    expect(target.searchParams.get('_r')).toMatch(/^\d+$/)
    wrapper.unmount()
  })

  it('reports the reload as busy and refuses a second click', async () => {
    const wrapper = mount(BrandMark)
    await wrapper.trigger('click')
    await flushPromises()

    expect(wrapper.attributes('aria-busy')).toBe('true')
    expect(wrapper.classes()).toContain('brand--refreshing')
    expect(wrapper.attributes('title')).toBe('Reloading...')

    await wrapper.trigger('click')
    await flushPromises()
    expect(replace).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })
})

describe('PaneHeader brand and page tag', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('puts the brand in its own column, outside the title', () => {
    const wrapper = mount(PaneHeader, { props: { pageTag: 'home' } })

    const centre = wrapper.get('.header-center')
    expect(centre.find('.brand').exists()).toBe(true)
    // Sibling of the title, not inside it: this is what makes the centring hold.
    expect(wrapper.find('.header-title .brand').exists()).toBe(false)
    expect(centre.element.parentElement).toBe(wrapper.get('.pane-header').element)
    wrapper.unmount()
  })

  it('keeps the brand in the middle column when the title is very long', () => {
    const wrapper = mount(PaneHeader, {
      props: { pageTag: 'project' },
      slots: { title: `<span class="pane-title">${LONG_TITLE}</span>` },
      global: { stubs: { transition: false } },
    })

    // The long title is present, and the header still reads title | centre | trail
    // in that order, with the brand in the centre column and nowhere else.
    expect(wrapper.get('.pane-title').text().length).toBeGreaterThan(200)
    const columns = Array.from(wrapper.get('.pane-header').element.children)
      .map(el => el.className)
      .filter(name => !name.includes('header-hamburger'))
    expect(columns).toEqual(['header-title', 'header-center', 'header-trail'])
    expect(wrapper.findAll('.brand')).toHaveLength(1)
    expect(wrapper.get('.header-center').find('.brand').exists()).toBe(true)
    wrapper.unmount()
  })

  it('renders the page tag it is given, and nothing when given none', () => {
    const tagged = mount(PaneHeader, { props: { pageTag: 'automations' } })
    expect(tagged.get('.page-tag').text()).toBe('automations')
    tagged.unmount()

    const untagged = mount(PaneHeader)
    expect(untagged.find('.page-tag').exists()).toBe(false)
    expect(untagged.find('.brand').exists()).toBe(true)
    untagged.unmount()
  })

  it('makes the tag the pane heading only when no title names the page', () => {
    const bare = mount(PaneHeader, { props: { pageTag: 'settings' } })
    expect(bare.get('.page-tag').element.tagName).toBe('H2')
    expect(bare.find('.header-title').exists()).toBe(false)
    bare.unmount()

    const titled = mount(PaneHeader, {
      props: { pageTag: 'project' },
      slots: { title: '<h2 class="project-title">Wedding</h2>' },
    })
    expect(titled.get('.page-tag').element.tagName).toBe('SPAN')
    expect(titled.findAll('h2')).toHaveLength(1)
    titled.unmount()
  })

  it('drops the brand where a second mark would duplicate the main pane', () => {
    const wrapper = mount(PaneHeader, { props: { brand: false } })
    expect(wrapper.find('.brand').exists()).toBe(false)
    expect(wrapper.find('.header-center').exists()).toBe(true)
    wrapper.unmount()
  })
})

describe('page tag per view', () => {
  const seen: { pageTag: string; brand: boolean }[] = []

  const RecordingPaneHeader = defineComponent({
    name: 'PaneHeader',
    props: {
      pageTag: { type: String, default: '' },
      brand: { type: Boolean, default: true },
    },
    setup(props) {
      seen.push({ pageTag: props.pageTag, brand: props.brand })
      return () => h('div', { class: 'pane-header-stub' })
    },
  })

  const EmptyStub = defineComponent({ name: 'EmptyStub', setup: () => () => h('div') })

  class MemoryStorage {
    private values = new Map<string, string>()
    getItem(key: string): string | null { return this.values.get(key) ?? null }
    setItem(key: string, value: string): void { this.values.set(key, value) }
    removeItem(key: string): void { this.values.delete(key) }
    clear(): void { this.values.clear() }
  }

  beforeEach(() => {
    seen.length = 0
    setActivePinia(createPinia())
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1200 })
    Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: new MemoryStorage() })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('tags the home screen "home" and gives it no separate title', async () => {
    const router = createRouter({
      history: createMemoryHistory(),
      routes: [{ path: '/', component: EmptyStub }],
    })
    await router.push('/')
    await router.isReady()

    const store = useProjectStore()
    store.bootstrapped = true
    store.activeChatId = null
    vi.spyOn(store, 'fetchAll').mockResolvedValue()

    const taskStore = useTaskStore()
    vi.spyOn(taskStore, 'fetchSchedules').mockResolvedValue()
    vi.spyOn(taskStore, 'fetchLoops').mockResolvedValue()

    const { default: ChatLayout } = await import('../ChatLayout.vue')
    const wrapper = mount(ChatLayout, {
      global: {
        plugins: [router],
        stubs: {
          ChatPanel: EmptyStub,
          ProjectSidebar: EmptyStub,
          ProjectView: EmptyStub,
          SchedulePanel: EmptyStub,
          SettingsView: EmptyStub,
          FileViewerModal: EmptyStub,
          PinnedFilePanel: EmptyStub,
          HomeRecentChats: EmptyStub,
          PaneHeader: RecordingPaneHeader,
        },
      },
    })
    await flushPromises()
    await nextTick()

    expect(seen).toEqual([{ pageTag: 'home', brand: true }])
    wrapper.unmount()
  })
})
