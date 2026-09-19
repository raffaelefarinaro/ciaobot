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
  /** Dispatch at raw CSS-pixel coordinates, for DPR-sensitive target tests. */
  function pointerTapAtCss(canvas: HTMLCanvasElement, cssX: number, cssY: number, pointerType = 'touch') {
    for (const type of ['pointerdown', 'pointerup'] as const) {
      canvas.dispatchEvent(new PointerEvent(type, {
        bubbles: true, pointerId: 1, isPrimary: true, pointerType,
        clientX: cssX, clientY: cssY,
      }))
    }
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

  it('keeps the touch target 44 CSS px at high DPR, not 44 device px', async () => {
    // The component multiplies pointer coordinates by devicePixelRatio before
    // screenToWorld, and camera.scale is device-pixel based, so a 44 CSS-pixel
    // token has to be converted to device pixels too. Without that, the target
    // shrinks to 44/dpr CSS px — the exact bug this pins at DPR 3.
    vi.stubGlobal('devicePixelRatio', 3)
    const { wrapper, mm, b } = await mountGraph()
    // Move B out of the way so the only possible hit is A.
    b.x = 100000
    const canvas = wrapper.find('canvas').element as HTMLCanvasElement

    // 15 CSS px right of node A: inside the fixed 22 CSS px radius, outside the
    // buggy 22/3 ≈ 7.3 CSS px radius and outside the painted node.
    pointerTapAtCss(canvas, 15, 0, 'touch')
    expect(mm.selectedId).toBe('a')
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

  it('groups the neighbor actions for wrapping at narrow panel widths', async () => {
    const { wrapper, mm } = await mountGraph()
    mm.selectNode('a')
    await nextTick()

    const row = wrapper.find('.mm-link-item')
    // The title and its actions are peers the stylesheet can wrap, rather than
    // inline buttons after the title. The wrapper is non-shrinking so the
    // actions keep a full touch target instead of being squeezed out of view.
    const actions = row.find('.mm-link-actions')
    expect(actions.exists()).toBe(true)
    expect(actions.findAll('.mm-path-btn')).toHaveLength(2)
    expect(actions.find('.mm-link-focus').exists()).toBe(true)
    // The title button is before the actions and remains the open control.
    expect(row.find('.mm-link-btn').attributes('aria-label')).toBe('Open Note B')
    wrapper.unmount()
  })
})

/**
 * List sorting.
 *
 * Every case here is a column that sorted a different value from the one its
 * cell renders, which `aria-sort` then described as sorted. None of it had
 * coverage, so CI would not have caught any of them.
 */
describe('MemoryMapView list sorting', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    apiGet.mockReset()
    apiGet.mockImplementation((url: string) => {
      if (url.includes('/api/vault/graph')) return Promise.resolve(sortPayload())
      return Promise.resolve({})
    })
    const store = useProjectStore()
    store.workspaces = [{ name: 'personal', vault_root: '/tmp/vault', default_provider: 'claude', gws_profile: '' }]
    store.activeWorkspace = 'personal'
    vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
    HTMLCanvasElement.prototype.getContext = vi.fn(() => null) as never
    vi.stubGlobal('requestAnimationFrame', vi.fn(() => 1))
    vi.stubGlobal('cancelAnimationFrame', vi.fn())
  })

  afterEach(async () => {
    document.body.innerHTML = ''
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  const nowSec = Math.floor(Date.now() / 1000)

  function sortPayload() {
    return {
      nodes: [
        // Lowercase title: a code-point sort puts it after every capital.
        { id: 'apple', title: 'apple notes', type: 'note', tags: [], aliases: [], description: '', workspace: 'personal', degree: 1, mtime: nowSec, updated: '2026-01-01', stale: false, age_days: 10 },
        { id: 'zebra', title: 'Zebra notes', type: 'note', tags: [], aliases: [], description: '', workspace: 'personal', degree: 1, mtime: nowSec, updated: '2026-01-01', stale: false, age_days: 20 },
        // No `updated:` — ageDays null, but a three-year-old mtime, so the
        // Checked cell still shows an age.
        { id: 'old', title: 'Ancient note', type: 'note', tags: [], aliases: [], description: '', workspace: 'personal', degree: 1, mtime: nowSec - 3 * 365 * 86400, updated: '', stale: false, age_days: null },
      ],
      edges: [{ source: 'apple', target: 'zebra' }],
    }
  }

  async function mountSortableList() {
    const wrapper = await mountView()
    await flushPromises()
    const mm = useMemoryMapStore()
    mm.view = 'list'
    await nextTick()
    return { wrapper, mm }
  }

  function titles(wrapper: ReturnType<typeof mount>): string[] {
    return wrapper.findAll('.mm-title-btn').map((b) => b.text())
  }

  function sortButton(wrapper: ReturnType<typeof mount>, label: string) {
    return wrapper.findAll('.mm-sort').find((b) => b.text().startsWith(label))!
  }

  it('sorts titles case-insensitively', async () => {
    // Name is the default sort key, so the list is already sorted ascending.
    const { wrapper } = await mountSortableList()

    const order = titles(wrapper)
    // 'apple notes' must not be exiled below every capitalised title.
    expect(order.indexOf('Ancient note')).toBeLessThan(order.indexOf('apple notes'))
    expect(order.indexOf('apple notes')).toBeLessThan(order.indexOf('Zebra notes'))
  })

  it('opens the Checked column oldest-first, for triage', async () => {
    const { wrapper } = await mountSortableList()
    await sortButton(wrapper, 'Checked').trigger('click')
    await nextTick()

    // The three-year-old note is the stalest, so it leads on the first click.
    expect(titles(wrapper)[0]).toBe('Ancient note')
  })

  it('ranks an undated note by the age its cell displays', async () => {
    const { wrapper } = await mountSortableList()
    const checked = sortButton(wrapper, 'Checked')
    await checked.trigger('click')
    await nextTick()
    await checked.trigger('click') // newest-first
    await nextTick()

    // Sorting on ageDays alone pinned it last in BOTH directions, below notes
    // whose cells showed a far more recent age.
    expect(titles(wrapper).at(-1)).toBe('Ancient note')
  })
})

/**
 * Review navigation: one tab bar, four sections.
 *
 * Review used to nest three tab rows — Proposals/Retirements here, then
 * Queue/History inside the proposal panel and To review/Trash inside the
 * retirement one — so the trash was a tab inside a tab inside a sidebar
 * button. These pin the flat bar, the labels that name the decision rather
 * than the pipeline, and the entry points that still have to land where they
 * always did.
 */
describe('MemoryMapView review navigation', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    apiGet.mockReset()
    apiGet.mockImplementation((url: string) => {
      if (url.includes('/api/vault/graph')) return Promise.resolve(graphPayload())
      if (url.startsWith('/api/proposals/history')) {
        return Promise.resolve({
          rows: [{
            id: 'h1', ts: '2026-09-01T10:00:00+00:00', action: 'accepted', via: 'pwa',
            kind: 'memory', text: 'Remember the thing', source: '', workspace: 'personal',
            destination: 'ciao:memory', outcome: 'written', proposal_id: 'p1',
          }],
          total: 500,
          truncated: true,
          limit: 200,
        })
      }
      if (url.startsWith('/api/proposals')) {
        return Promise.resolve({
          rows: [{
            id: 'p1', kind: 'memory', text: 'A queued fact', source: '', path: '',
            workspace: 'personal', region: 'memory', target: '', ts: '2026-09-01',
          }],
        })
      }
      if (url.startsWith('/api/vault/review')) {
        return Promise.resolve({
          candidates: [{
            candidate_id: 'c1', workspace: 'personal', path: 'memory-vault/People/Mo.md',
            content_hash: 'deadbeef', signals: ['unlinked'], priority: 1,
            evidence: {
              backlinks: [], outbound_links: [], bridge: false, duplicate_group: [],
              last_update: '', type: 'note', age_days: null,
            },
            status: 'candidate', disposition: '', deferred_until: '',
          }],
          trashed: [{
            candidate_id: 't1', workspace: 'personal',
            original_path: 'memory-vault/People/Old.md', content_hash: 'cafef00d',
            trashed_at: '2026-09-01T00:00:00Z',
          }],
          cleared: [],
        })
      }
      return Promise.resolve({})
    })
    const store = useProjectStore()
    store.workspaces = [{ name: 'personal', vault_root: '/tmp/vault', default_provider: 'claude', gws_profile: '' }]
    store.activeWorkspace = 'personal'
    vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
    HTMLCanvasElement.prototype.getContext = vi.fn(() => null) as never
    vi.stubGlobal('requestAnimationFrame', vi.fn(() => 1))
    vi.stubGlobal('cancelAnimationFrame', vi.fn())
  })

  afterEach(() => {
    document.body.innerHTML = ''
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  async function mountReview() {
    const wrapper = await mountView()
    const mm = useMemoryMapStore()
    mm.view = 'review'
    await flushPromises()
    await nextTick()
    await flushPromises()
    return { wrapper, mm }
  }

  function tabs(wrapper: ReturnType<typeof mount>) {
    return wrapper.findAll('[role="tab"]')
  }

  it('renders exactly one tab bar, holding all four sections', async () => {
    const { wrapper } = await mountReview()

    expect(wrapper.findAll('[role="tablist"]')).toHaveLength(1)
    expect(tabs(wrapper).map(t => t.text().replace(/\d+$/, '').trim())).toEqual([
      'Suggested memories', 'Notes to revisit', 'Retired', 'History',
    ])
    wrapper.unmount()
  })

  it('reaches the retired notes in one click, not a tab inside a tab', async () => {
    const { wrapper, mm } = await mountReview()

    await tabs(wrapper)[2]!.trigger('click')
    await flushPromises()

    expect(mm.reviewTab).toBe('retirement')
    expect(mm.retirementTab).toBe('trash')
    expect(wrapper.text()).toContain('Old')
    wrapper.unmount()
  })

  it('shows the decision ledger from the same bar', async () => {
    const { wrapper } = await mountReview()

    await tabs(wrapper)[3]!.trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('Remember the thing')
    wrapper.unmount()
  })

  it('selects the notes-to-revisit tab when a stale note asks for it', async () => {
    // The stale note's detail panel and the sidebar's "Needs review" list both
    // set the old two-part state; flattening the bar may not break them.
    const { wrapper, mm } = await mountReview()
    mm.reviewTab = 'retirement'
    mm.retirementTab = 'candidates'
    await nextTick()

    expect(tabs(wrapper)[1]!.attributes('aria-selected')).toBe('true')
    expect(wrapper.text()).toContain('Mo')
    wrapper.unmount()
  })

  it('counts each queue on its own tab, scoped to the workspace', async () => {
    const { wrapper } = await mountReview()

    const counts = tabs(wrapper).map(t => (
      t.find('.tab-bar-count').exists() ? t.find('.tab-bar-count').text() : null
    ))
    // One queued proposal, one candidate, one retired note, and the ledger's
    // server-side total rather than the page size.
    expect(counts).toEqual(['1', '1', '1', '500'])
    wrapper.unmount()
  })

  it('renders no History count while the ledger is still unloaded', async () => {
    // Null, not zero: "History 0" on a ledger with hundreds of rows is the
    // opposite of what a badge is for.
    apiGet.mockImplementation((url: string) => {
      if (url.includes('/api/vault/graph')) return Promise.resolve(graphPayload())
      if (url.startsWith('/api/proposals/history')) return Promise.reject(new Error('nope'))
      if (url.startsWith('/api/proposals')) return Promise.resolve({ rows: [] })
      return Promise.resolve({ candidates: [], trashed: [], cleared: [] })
    })
    const { wrapper } = await mountReview()

    expect(tabs(wrapper)[3]!.find('.tab-bar-count').exists()).toBe(false)
    wrapper.unmount()
  })
})
