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

  /** Mount the graph surface with the canvas attached so pointer handlers are
   * live, and place the two nodes at known world positions. The camera frames a
   * zero-sized canvas, so it stays at the origin with DEFAULT_SCALE (0.55); a
   * node at world (x, 0) therefore lands at screen x * 0.55. */
  async function mountGraph() {
    const wrapper = await mountView()
    await flushPromises()
    await nextTick()
    const mm = useMemoryMapStore()
    const a = mm.nodes.find(n => n.id === 'a')!
    const b = mm.nodes.find(n => n.id === 'b')!
    a.x = 0; a.y = 0; a.vx = 0; a.vy = 0
    b.x = 100; b.y = 0; b.vx = 0; b.vy = 0
    return { wrapper, mm, a, b }
  }

  const SCALE = 0.55
  function pointerDown(canvas: HTMLCanvasElement, pointerId: number, worldX: number, worldY = 0, isPrimary = true, pointerType = 'touch') {
    canvas.dispatchEvent(new PointerEvent('pointerdown', {
      bubbles: true, pointerId, isPrimary, pointerType,
      clientX: worldX * SCALE, clientY: worldY * SCALE,
    }))
  }
  function pointerMove(canvas: HTMLCanvasElement, pointerId: number, worldX: number, worldY = 0) {
    canvas.dispatchEvent(new PointerEvent('pointermove', {
      bubbles: true, pointerId, clientX: worldX * SCALE, clientY: worldY * SCALE,
    }))
  }
  function pointerUp(canvas: HTMLCanvasElement, pointerId: number, worldX: number, worldY = 0, isPrimary = true, pointerType = 'touch') {
    canvas.dispatchEvent(new PointerEvent('pointerup', {
      bubbles: true, pointerId, isPrimary, pointerType,
      clientX: worldX * SCALE, clientY: worldY * SCALE,
    }))
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

  it('keeps dragging the first finger\'s node when a second pointer lands on another node', async () => {
    const { wrapper, a, b } = await mountGraph()
    const canvas = wrapper.find('canvas').element as HTMLCanvasElement

    // First finger grabs node A and starts dragging it.
    pointerDown(canvas, 1, 0)
    pointerMove(canvas, 1, 20)
    expect(a.x).toBeCloseTo(20)

    // Second finger lands on node B. The gesture model rejects it, and the
    // component must not adopt B as the drag target.
    pointerDown(canvas, 2, 100)
    // The first finger keeps dragging A, not B...
    pointerMove(canvas, 1, 40)
    expect(a.x).toBeCloseTo(40)
    expect(b.x).toBe(100)

    // ...and the ignored pointer's release must not clear A.
    pointerUp(canvas, 2, 100)
    pointerMove(canvas, 1, 60)
    expect(a.x).toBeCloseTo(60)
    expect(b.x).toBe(100)

    pointerUp(canvas, 1, 60)
    wrapper.unmount()
  })

  it('does not let an ignored second pointer drag the node it landed on', async () => {
    const { wrapper, a, b } = await mountGraph()
    const canvas = wrapper.find('canvas').element as HTMLCanvasElement

    pointerDown(canvas, 1, 0)
    pointerMove(canvas, 1, 20)
    pointerDown(canvas, 2, 100)

    // Moving the second finger must never move B, and must not move A either.
    pointerMove(canvas, 2, 130)
    expect(b.x).toBe(100)
    expect(a.x).toBeCloseTo(20)

    pointerUp(canvas, 2, 130)
    // The first gesture is still live and can finish normally.
    pointerMove(canvas, 1, 45)
    expect(a.x).toBeCloseTo(45)
    expect(b.x).toBe(100)
    pointerUp(canvas, 1, 45)
    wrapper.unmount()
  })

  it('still selects a node on a clean tap after an ignored pointer is gone', async () => {
    const { wrapper, mm } = await mountGraph()
    const canvas = wrapper.find('canvas').element as HTMLCanvasElement

    pointerDown(canvas, 1, 0)
    pointerUp(canvas, 1, 0)
    expect(mm.selectedId).toBe('a')

    // A later gesture from another pointer behaves normally.
    pointerDown(canvas, 2, 100, 0, false)
    pointerUp(canvas, 2, 100, 0, false)
    expect(mm.selectedId).toBe('a')

    pointerDown(canvas, 3, 100)
    pointerUp(canvas, 3, 100)
    expect(mm.selectedId).toBe('b')
    wrapper.unmount()
  })

  it('gives a touch tap a 44px target outside the painted node', async () => {
    const { wrapper, mm } = await mountGraph()
    const canvas = wrapper.find('canvas').element as HTMLCanvasElement

    // World 30 from node A is outside its painted hit radius at fit scale
    // (~12 world units) but inside the touch floor (~40 world units).
    pointerDown(canvas, 1, 30, 0, true, 'touch')
    pointerUp(canvas, 1, 30, 0, true, 'touch')
    expect(mm.selectedId).toBe('a')
    wrapper.unmount()
  })

  it('keeps the precise painted target for a mouse click', async () => {
    const { wrapper, mm } = await mountGraph()
    const canvas = wrapper.find('canvas').element as HTMLCanvasElement

    pointerDown(canvas, 1, 30, 0, true, 'mouse')
    pointerUp(canvas, 1, 30, 0, true, 'mouse')
    // A mouse keeps the painted hit area: 30 world units from the node misses.
    expect(mm.selectedId).toBeNull()
    wrapper.unmount()
  })

  it('returns focus to the graph region when a neighbor link unmounts', async () => {
    const { wrapper, mm } = await mountGraph()
    // Open A so its neighbor list (with B) renders in the detail panel.
    mm.selectNode('a')
    await nextTick()

    const neighbor = wrapper.find('.mm-link-btn')
    expect(neighbor.exists()).toBe(true)
    ;(neighbor.element as HTMLElement).focus()
    await neighbor.trigger('click')
    await nextTick()
    // The panel now shows B, so the link that opened it is gone.
    expect(mm.selectedId).toBe('b')

    await wrapper.find('.mm-detail-close').trigger('click')
    await flushPromises()
    await nextTick()
    await nextTick()

    // No list title exists in graph view, so focus lands on the graph region
    // rather than the document body.
    const canvasWrap = wrapper.find('.mm-canvas-wrap').element
    expect(document.activeElement).toBe(canvasWrap)
    expect(document.activeElement).not.toBe(document.body)
    wrapper.unmount()
  })
})
