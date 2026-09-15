// @vitest-environment jsdom

/**
 * Keyboard and touch reachability of the Memory page.
 *
 * The list used to open a note only through a `<tr @click>`, which no keyboard
 * can reach; the canvas used mouse-only handlers. These tests pin the
 * replacements: a native title button, named path controls, and neighbor links
 * that are real buttons. The pointer state machine itself is covered by
 * `lib/graphGesture.test.ts`; here we assert the DOM a keyboard user drives.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import MemoryMapView from '../MemoryMapView.vue'
import { useMemoryMapStore } from '../../stores/memoryMap'
import { useProjectStore } from '../../stores/projects'

const apiGet = vi.hoisted(() => vi.fn())

vi.mock('../../lib/api', () => ({
  api: { get: apiGet, post: vi.fn(), patch: vi.fn(), del: vi.fn() },
}))

const Stub = { template: '<div />' }

async function mountView() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: Stub },
      { path: '/memory', component: Stub },
      { path: '/proposals', component: Stub },
    ],
  })
  await router.push('/memory')
  await router.isReady()
  return mount(MemoryMapView, {
    attachTo: document.body,
    global: { plugins: [router] },
  })
}

function graphPayload() {
  return {
    nodes: [
      { id: 'a', title: 'Note A', type: 'note', tags: [], aliases: [], description: '', workspace: 'personal', degree: 1, mtime: 1, updated: '2026-01-01', stale: false, age_days: 3 },
      { id: 'b', title: 'Note B', type: 'note', tags: [], aliases: [], description: '', workspace: 'personal', degree: 1, mtime: 2, updated: '2026-01-01', stale: false, age_days: 4 },
    ],
    edges: [{ source: 'a', target: 'b' }],
  }
}

describe('MemoryMapView keyboard and touch access', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    apiGet.mockReset()
    apiGet.mockImplementation((url: string) => {
      if (url.includes('/api/vault/graph')) return Promise.resolve(graphPayload())
      return Promise.resolve({})
    })
    const store = useProjectStore()
    store.workspaces = [{ name: 'personal', vault_root: '/tmp/vault', default_provider: 'claude', gws_profile: '' }]
    store.activeWorkspace = 'personal'
    // jsdom has neither, and attachCanvas constructs both.
    vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
    HTMLCanvasElement.prototype.getContext = vi.fn(() => null) as never
    // jsdom's rAF is real but never paints here; a no-op keeps the loop quiet.
    vi.stubGlobal('requestAnimationFrame', vi.fn(() => 1))
    vi.stubGlobal('cancelAnimationFrame', vi.fn())
  })

  afterEach(async () => {
    document.body.innerHTML = ''
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  async function mountList() {
    const wrapper = await mountView()
    await flushPromises()
    const mm = useMemoryMapStore()
    mm.view = 'list'
    await nextTick()
    return { wrapper, mm }
  }

  it('opens a note from a native title button, not just a row click', async () => {
    const { wrapper, mm } = await mountList()
    const titles = wrapper.findAll('.mm-title-btn')
    expect(titles).toHaveLength(2)
    expect(titles[0].attributes('aria-label')).toBe('Open Note A')

    await titles[0].trigger('click')
    expect(mm.selectedId).toBe('a')
    wrapper.unmount()
  })

  it('exposes the selected state on the title control', async () => {
    const { wrapper, mm } = await mountList()
    const first = wrapper.findAll('.mm-title-btn')[0]
    expect(first.attributes('aria-pressed')).toBe('false')
    mm.selectNode('a')
    await nextTick()
    expect(wrapper.findAll('.mm-title-btn')[0].attributes('aria-pressed')).toBe('true')
    wrapper.unmount()
  })

  it('sets both path endpoints from named row controls', async () => {
    const { wrapper, mm } = await mountList()
    const rows = wrapper.findAll('.mm-list-wrap tbody tr')
    const startA = rows[0].findAll('.mm-path-btn')[0]
    const endB = rows[1].findAll('.mm-path-btn')[1]
    expect(startA.attributes('aria-label')).toBe('Set Note A as path start')
    expect(endB.attributes('aria-label')).toBe('Set Note B as path end')

    await startA.trigger('click')
    await endB.trigger('click')
    expect(mm.pathStart).toBe('a')
    expect(mm.pathEnd).toBe('b')
    expect(mm.pathIds.has('a')).toBe(true)
    expect(mm.pathIds.has('b')).toBe(true)
    wrapper.unmount()
  })

  it('returns focus to the title control after the detail panel closes', async () => {
    const { wrapper } = await mountList()
    const first = wrapper.findAll('.mm-title-btn')[0]
    ;(first.element as HTMLElement).focus()
    await first.trigger('click')
    await nextTick()

    await wrapper.find('.mm-detail-close').trigger('click')
    await flushPromises()
    await nextTick()
    await nextTick()

    expect(document.activeElement).toBe(first.element)
    wrapper.unmount()
  })

  it('renders a neighbor as a named button and offers its path controls', async () => {
    const { wrapper, mm } = await mountList()
    await wrapper.findAll('.mm-title-btn')[0].trigger('click')
    await nextTick()

    const neighbor = wrapper.find('.mm-link-btn')
    expect(neighbor.exists()).toBe(true)
    expect(neighbor.attributes('aria-label')).toBe('Open Note B')

    const detail = wrapper.find('.mm-detail')
    const starts = detail.findAll('.mm-path-btn')
    expect(starts.map(b => b.attributes('aria-label'))).toEqual([
      'Set Note B as path start',
      'Set Note B as path end',
    ])
    await starts[0].trigger('click')
    expect(mm.pathStart).toBe('b')
    wrapper.unmount()
  })

  it('offers explicit path controls for the focused note in the detail panel', async () => {
    const { wrapper, mm } = await mountList()
    await wrapper.findAll('.mm-title-btn')[0].trigger('click')
    await nextTick()

    const controls = wrapper.find('.mm-detail-path-controls')
    expect(controls.exists()).toBe(true)
    const buttons = controls.findAll('.btn-chip')
    expect(buttons.map(b => b.text())).toEqual(['Start path', 'End path'])
    await buttons[0].trigger('click')
    expect(mm.pathStart).toBe('a')
    // The hint explains the state in words, not just colour.
    expect(wrapper.find('.mm-detail-path-hint').text()).toContain('Start: Note A')
    wrapper.unmount()
  })
})
