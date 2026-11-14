// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { flushPromises, mount } from '@vue/test-utils'
import { defineComponent, h, nextTick } from 'vue'
import { useProjectStore } from '../../stores/projects'
import { useTaskStore } from '../../stores/tasks'
import BrandMark from '../BrandMark.vue'
import PaneHeader from '../PaneHeader.vue'

const LONG_TITLE = 'A chat title long enough to need every pixel of the header and then a great many more '.repeat(3)

describe('BrandMark', () => {
  const originalLocation = Object.getOwnPropertyDescriptor(window, 'location')
  const originalServiceWorker = Object.getOwnPropertyDescriptor(navigator, 'serviceWorker')
  let replace: ReturnType<typeof vi.fn>

  beforeEach(() => {
    replace = vi.fn()
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { href: 'http://localhost/chat/abc', replace },
    })
  })

  afterEach(() => {
    if (originalLocation) Object.defineProperty(window, 'location', originalLocation)
    if (originalServiceWorker) Object.defineProperty(navigator, 'serviceWorker', originalServiceWorker)
    else Reflect.deleteProperty(navigator, 'serviceWorker')
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('renders the supplied static icon', () => {
    const wrapper = mount(BrandMark)
    expect(wrapper.element.tagName).toBe('IMG')
    expect(wrapper.attributes('src')).toBe('/icons/icon-32.png')
    expect(wrapper.attributes('alt')).toBe('Ciaobot')
    wrapper.unmount()
  })

  it('is not a focusable reload control', () => {
    const wrapper = mount(BrandMark)
    expect(wrapper.element.tagName).toBe('IMG')
    expect(wrapper.attributes('tabindex')).toBeUndefined()
    expect(wrapper.attributes('role')).toBeUndefined()
    expect(wrapper.attributes('aria-label')).toBeUndefined()
    expect(wrapper.attributes('aria-busy')).toBeUndefined()
    expect(wrapper.attributes('title')).toBeUndefined()
    wrapper.unmount()
  })

  it('does not navigate, clear caches or update service workers when clicked', async () => {
    const update = vi.fn()
    const deleteCache = vi.fn()
    const keys = vi.fn().mockResolvedValue(['app'])
    const getRegistrations = vi.fn().mockResolvedValue([{ update }])
    vi.stubGlobal('caches', { keys, delete: deleteCache })
    Object.defineProperty(navigator, 'serviceWorker', {
      configurable: true, value: Object.assign(new EventTarget(), { getRegistrations }),
    })
    const wrapper = mount(BrandMark)
    await wrapper.trigger('click')
    await flushPromises()
    expect(replace).not.toHaveBeenCalled()
    expect(keys).not.toHaveBeenCalled()
    expect(deleteCache).not.toHaveBeenCalled()
    expect(getRegistrations).not.toHaveBeenCalled()
    expect(update).not.toHaveBeenCalled()
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
      .filter(name => !name.includes('header-lead'))
    expect(columns).toEqual(['header-title', 'header-center header-center--with-title', 'header-trail'])
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

  it('phone-overflow adds the overflow class', () => {
    const plain = mount(PaneHeader)
    expect(plain.classes()).not.toContain('pane-header--overflow')
    plain.unmount()

    const overflow = mount(PaneHeader, { props: { phoneOverflow: true } })
    expect(overflow.classes()).toContain('pane-header--overflow')
    overflow.unmount()
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

  it('tags the home screen "Home" and gives it no separate title', async () => {
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

    expect(seen).toEqual([{ pageTag: 'Home', brand: true }])
    wrapper.unmount()
  })
})
