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
  let frames: FrameRequestCallback[]

  function runFrames(count: number) {
    for (let i = 0; i < count; i++) frames.splice(0).forEach((cb) => cb(0))
  }
  const progressOf = (wrapper: ReturnType<typeof mount>) => wrapper.getComponent({ name: 'CiaoMark' }).props('progress') as number

  beforeEach(() => {
    replace = vi.fn()
    frames = []
    Object.defineProperty(window, 'location', {
      configurable: true,
      value: { href: 'http://localhost/chat/abc', replace },
    })
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => frames.push(cb))
    vi.stubGlobal('cancelAnimationFrame', () => { frames = [] })
    vi.stubGlobal('matchMedia', () => ({ matches: false }))
  })

  afterEach(() => {
    if (originalLocation) Object.defineProperty(window, 'location', originalLocation)
    if (originalServiceWorker) Object.defineProperty(navigator, 'serviceWorker', originalServiceWorker)
    else Reflect.deleteProperty(navigator, 'serviceWorker')
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('renders the inline vector mark with a themeable C and accent dots', () => {
    const wrapper = mount(BrandMark)
    expect(wrapper.get('svg path').attributes('stroke')).toBe('currentColor')
    expect(wrapper.findAll('svg circle')).toHaveLength(2)
    expect(wrapper.findAll('svg circle').map((circle) => circle.attributes('fill'))).toEqual(['var(--accent)', 'var(--accent)'])
    expect(wrapper.find('img').exists()).toBe(false)
    wrapper.unmount()
  })

  it('is a labelled reload button', () => {
    const wrapper = mount(BrandMark)
    expect(wrapper.element.tagName).toBe('BUTTON')
    expect(wrapper.attributes('aria-label')).toBe('Reload Ciaobot')
    expect(wrapper.attributes('type')).toBe('button')
    wrapper.unmount()
  })

  it('closes slowly while hovered and reopens from where it stopped when the pointer leaves', async () => {
    const wrapper = mount(BrandMark)
    expect(progressOf(wrapper)).toBe(0)
    await wrapper.trigger('pointerenter')
    runFrames(20)
    await nextTick()
    const midway = progressOf(wrapper)
    expect(midway).toBeGreaterThan(0.2)
    expect(midway).toBeLessThan(0.8)
    await wrapper.trigger('pointerleave')
    runFrames(3)
    await nextTick()
    expect(progressOf(wrapper)).toBeLessThan(midway)
    expect(progressOf(wrapper)).toBeGreaterThan(0)
    runFrames(400)
    await nextTick()
    expect(progressOf(wrapper)).toBe(0)
    wrapper.unmount()
  })

  it('reaches the closed ring if the pointer stays, and follows keyboard focus', async () => {
    const wrapper = mount(BrandMark)
    await wrapper.trigger('focus')
    runFrames(600)
    await nextTick()
    expect(progressOf(wrapper)).toBe(1)
    await wrapper.trigger('blur')
    runFrames(600)
    await nextTick()
    expect(progressOf(wrapper)).toBe(0)
    wrapper.unmount()
  })

  it('stays a static C with reduced motion', async () => {
    vi.stubGlobal('matchMedia', () => ({ matches: true }))
    const wrapper = mount(BrandMark)
    await wrapper.trigger('pointerenter')
    runFrames(50)
    await nextTick()
    expect(progressOf(wrapper)).toBe(0)
    expect(frames).toHaveLength(0)
    wrapper.unmount()
  })

  it('hard-refreshes on click: updates service workers, clears caches, reloads with a cache-busting query', async () => {
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
    expect(update).toHaveBeenCalledOnce()
    expect(deleteCache).toHaveBeenCalledWith('app')
    expect(replace).toHaveBeenCalledOnce()
    const target = new URL(String(replace.mock.calls[0][0]))
    expect(target.pathname).toBe('/chat/abc')
    expect(target.searchParams.get('_r')).toMatch(/^\d+$/)
    wrapper.unmount()
  })

  it('reloads once even if clicked twice', async () => {
    const wrapper = mount(BrandMark)
    await wrapper.trigger('click')
    await wrapper.trigger('click')
    await flushPromises()
    expect(replace).toHaveBeenCalledOnce()
    wrapper.unmount()
  })
})

describe('PaneHeader page tag, title and controls', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('keeps the page tag without owning a brand image or prop', () => {
    const wrapper = mount(PaneHeader, { props: { pageTag: 'home' } })

    const centre = wrapper.get('.header-center')
    expect(centre.get('.page-tag').text()).toBe('home')
    expect(wrapper.find('img').exists()).toBe(false)
    expect(wrapper.vm.$options.props).not.toHaveProperty('brand')
    expect(wrapper.classes()).not.toContain('pane-header--no-center')
    expect(centre.element.parentElement).toBe(wrapper.get('.pane-header').element)
    wrapper.unmount()
  })

  it('keeps a long title, page tag and working action in separate regions', async () => {
    const action = vi.fn()
    const wrapper = mount(PaneHeader, {
      props: { pageTag: 'project' },
      slots: {
        title: `<span class="pane-title">${LONG_TITLE}</span>`,
        actions: () => h('button', { onClick: action }, 'Run now'),
      },
      global: { stubs: { transition: false } },
    })

    expect(wrapper.get('.pane-title').text()).toBe(LONG_TITLE.trim())
    const columns = Array.from(wrapper.get('.pane-header').element.children)
      .map(el => el.className)
      .filter(name => !name.includes('header-lead'))
    expect(columns).toEqual(['header-title', 'header-center header-center--with-title', 'header-trail'])
    expect(wrapper.get('.header-center .page-tag').text()).toBe('project')
    expect(wrapper.find('img').exists()).toBe(false)
    await wrapper.get('.header-actions button').trigger('click')
    expect(action).toHaveBeenCalledOnce()
    wrapper.unmount()
  })

  it('renders the page tag it is given, and nothing when given none', () => {
    const tagged = mount(PaneHeader, { props: { pageTag: 'automations' } })
    expect(tagged.get('.page-tag').text()).toBe('automations')
    tagged.unmount()

    const untagged = mount(PaneHeader)
    expect(untagged.find('.page-tag').exists()).toBe(false)
    expect(untagged.find('img').exists()).toBe(false)
    expect(untagged.classes()).toContain('pane-header--no-center')
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

  it('gives an item title the no-center layout and retains mobile navigation', async () => {
    const wrapper = mount(PaneHeader, {
      slots: { title: '<h2 class="pane-title">An item</h2>' },
    })
    expect(wrapper.classes()).toContain('pane-header--no-center')
    expect(wrapper.get('h2').text()).toBe('An item')
    expect(wrapper.find('.header-center').exists()).toBe(true)
    expect(wrapper.find('img').exists()).toBe(false)
    await wrapper.get('button[aria-label="Open sidebar"]').trigger('click')
    expect(wrapper.emitted('open-sidebar')).toHaveLength(1)
    expect(wrapper.find('.header-bell').exists()).toBe(true)
    wrapper.unmount()
  })
})

describe('PaneHeader compact page contract', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('marks a page-only header while retaining its dynamic semantic heading', async () => {
    const wrapper = mount(PaneHeader, { props: { pageTag: 'Appearance' } })
    expect(wrapper.classes()).toContain('pane-header--compact-page')
    expect(wrapper.classes()).not.toContain('pane-header--page-actions')
    expect(wrapper.get('h2.page-tag').text()).toBe('Appearance')
    expect(wrapper.find('.header-title').exists()).toBe(false)
    expect(wrapper.attributes('aria-hidden')).toBeUndefined()
    await wrapper.setProps({ pageTag: 'Overview' })
    expect(wrapper.get('h2.page-tag').text()).toBe('Overview')
    await wrapper.get('button[aria-label="Open sidebar"]').trigger('click')
    expect(wrapper.emitted('open-sidebar')).toHaveLength(1)
    wrapper.unmount()
  })

  it('marks the actions slot without replacing the page heading or its button', async () => {
    const action = vi.fn()
    const wrapper = mount(PaneHeader, {
      props: { pageTag: 'Tasks' },
      slots: { actions: () => h('button', { onClick: action }, 'New task') },
    })
    expect(wrapper.classes()).toContain('pane-header--compact-page')
    expect(wrapper.classes()).toContain('pane-header--page-actions')
    expect(wrapper.get('h2.page-tag').text()).toBe('Tasks')
    await wrapper.get('.header-actions button').trigger('click')
    expect(action).toHaveBeenCalledOnce()
    wrapper.unmount()
  })

  it('never compacts an item title or an untagged header', () => {
    const titled = mount(PaneHeader, {
      props: { pageTag: 'Project' },
      slots: { title: '<h2>Offsite</h2>', actions: '<button>Close project</button>' },
    })
    expect(titled.classes()).not.toContain('pane-header--compact-page')
    expect(titled.get('.page-tag').element.tagName).toBe('SPAN')
    expect(titled.get('h2').text()).toBe('Offsite')
    titled.unmount()
    const untagged = mount(PaneHeader)
    expect(untagged.classes()).not.toContain('pane-header--compact-page')
    untagged.unmount()
  })
})

describe('page tag per view', () => {
  const seen: { pageTag: string }[] = []

  const RecordingPaneHeader = defineComponent({
    name: 'PaneHeader',
    props: {
      pageTag: { type: String, default: '' },
    },
    setup(props) {
      seen.push({ pageTag: props.pageTag })
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

    expect(seen).toEqual([{ pageTag: 'Home' }])
    wrapper.unmount()
  })
})
