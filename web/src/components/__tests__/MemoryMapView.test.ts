// @vitest-environment jsdom

/**
 * Keyboard and touch reachability of the Memory page.
 *
 * The list used to open a note only through a `<tr @click>`, which no keyboard
 * can reach; the canvas used mouse-only handlers. These tests pin the
 * replacements: a native title button and neighbor links that are real
 * buttons. The pointer state machine itself is covered by
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
import { useProposalsStore } from '../../stores/proposals'

const apiGet = vi.hoisted(() => vi.fn())
const apiPost = vi.hoisted(() => vi.fn())

vi.mock('../../lib/api', () => ({
  api: { get: apiGet, post: apiPost, patch: vi.fn(), del: vi.fn() },
}))

const Stub = { template: '<div />' }
// The note tile is the pinned-file panel, which fetches and renders the file;
// here it only has to show the slots the map fills and emit its close.
const PinnedFilePanelStub = {
  props: ['filePath', 'closeLabel', 'inMemoryMap'],
  emits: ['close'],
  template: `<div class="pfp-stub" :data-file="filePath">
    <button type="button" class="pfp-close" :aria-label="closeLabel" @click="$emit('close')">x</button>
    <slot name="lead" /><slot name="after" />
  </div>`,
}

async function mountView(path = '/memory/map') {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: Stub },
      { path: '/memory/:section?', component: Stub },
    ],
  })
  await router.push(path)
  await router.isReady()
  const wrapper = mount(MemoryMapView, {
    attachTo: document.body,
    global: { plugins: [router], stubs: { PinnedFilePanel: PinnedFilePanelStub } },
  })
  return Object.assign(wrapper, { router })
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

function checkedPayload() {
  const base = (over: Record<string, unknown> = {}) => ({
    tags: [], aliases: [], description: '', workspace: 'personal', degree: 1,
    mtime: 1, updated: '2025-01-01', age_days: 640, threshold_days: 180, ...over,
  })
  return {
    nodes: [
      base({
        id: 'checked', title: 'Checked', type: 'note', stale: false,
        check: { outcome: 'still_valid', checked_at: '2026-09-28', retry_after: '2026-10-28', coverage: 'complete', reason: '', citations: 3, receipt_id: 'mrcpt_1', settled: true, pending: false, proposal_id: '', conflicted: false },
      }),
      base({
        id: 'partial', title: 'Partial', type: 'note', stale: false,
        check: { outcome: 'unverified', checked_at: '2026-09-28', retry_after: '2026-10-28', coverage: 'partial', reason: 'a connector was down', citations: 0, receipt_id: '', settled: true, pending: false, proposal_id: '', conflicted: false },
      }),
      base({
        id: 'pending', title: 'Pending', type: 'note', stale: false,
        check: { outcome: 'retire', checked_at: '2026-09-28', retry_after: '2026-10-28', coverage: 'complete', reason: 'the office moved', citations: 2, receipt_id: '', settled: true, pending: true, proposal_id: 'p-1', conflicted: false },
      }),
      base({
        id: 'conflicted', title: 'Conflicted', type: 'note', stale: true,
        check: { outcome: 'retire', checked_at: '2026-08-01', retry_after: '2026-08-31', coverage: 'complete', reason: 'the office moved', citations: 2, receipt_id: '', settled: false, pending: false, proposal_id: 'p-2', conflicted: true },
      }),
      base({ id: 'untouched', title: 'Untouched', type: 'note', stale: true, check: null }),
    ],
    edges: [],
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
    const mm = useMemoryMapStore()
    mm.view = 'graph'
    await nextTick()
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

  it('offers no path finder anywhere: rows, tile and canvas carry no start/end', async () => {
    const { wrapper, mm } = await mountList()
    expect(wrapper.find('.mm-path-btn').exists()).toBe(false)
    expect(wrapper.find('.mm-row-menu-btn').exists()).toBe(false)
    mm.selectNode('a')
    await nextTick()
    expect(wrapper.text()).not.toMatch(/start path|end path/i)
    expect('pathStart' in mm).toBe(false)
    wrapper.unmount()
  })

  it('puts the vault numbers in the toolbar, with no rail beside the map', async () => {
    const { wrapper } = await mountList()
    expect(wrapper.find('.page-rail').exists()).toBe(false)
    const stats = wrapper.get('.mm-toolbar-stats').text()
    expect(stats).toContain('2 notes')
    expect(stats).toContain('1 links')
    // One orphan menu replaces the two toggle buttons.
    const filter = wrapper.get<HTMLSelectElement>('#mm-orphan-filter')
    expect(filter.findAll('option').map(o => o.text())).toEqual(['All notes', 'Linked only', 'Orphans only'])
    await filter.setValue('only')
    expect(useMemoryMapStore().orphanFilter).toBe('only')
    wrapper.unmount()
  })

  it('opens the selected note in the docked tile, with its links under it', async () => {
    const { wrapper, mm } = await mountList()
    mm.selectNode('a')
    await nextTick()
    const tile = wrapper.get('.mm-tile')
    expect(tile.get('.pfp-stub').attributes('data-file')).toBe('a')
    expect(tile.get('.pfp-close').attributes('aria-label')).toBe('Close note')
    const link = tile.get('.mm-tile-link')
    expect(link.attributes('aria-label')).toBe('Show Note B on the map')
    await link.trigger('click')
    expect(mm.selectedId).toBe('b')
    wrapper.unmount()
  })

  it('returns focus to the title control after the detail panel closes', async () => {
    const { wrapper } = await mountList()
    const first = wrapper.findAll('.mm-title-btn')[0]
    ;(first.element as HTMLElement).focus()
    await first.trigger('click')
    await nextTick()

    await wrapper.find('.pfp-close').trigger('click')
    await flushPromises()
    await nextTick()
    await nextTick()

    expect(document.activeElement).toBe(first.element)
    wrapper.unmount()
  })

  it('drops the stored return control when the selection moves to another note', async () => {
    // The return control is recorded by activateRow/openNeighbor, but the
    // selection also moves through the canvas, focusNode, openNoteFile, the
    // pending-focus hand-off and the sidebar's requestFocus — none of which
    // touch it. A leftover entry pointed at Note A's title button, which is
    // still mounted in the list, so closing Note B's panel threw focus onto
    // the wrong note's control.
    const { wrapper, mm } = await mountList()
    const first = wrapper.findAll('.mm-title-btn')[0]
    ;(first.element as HTMLElement).focus()
    await first.trigger('click')
    await nextTick()
    expect(mm.selectedId).toBe('a')

    // The sidebar (or a canvas tap) moves the panel to Note B.
    mm.requestFocus('b')
    await nextTick()
    expect(mm.selectedId).toBe('b')

    const close = wrapper.find('.pfp-close')
    ;(close.element as HTMLElement).focus()
    await close.trigger('click')
    await flushPromises()
    await nextTick()
    await nextTick()

    // Note A's title button is still mounted in the list, so the stale entry
    // would have been restored to it. Focus belongs to the note that was
    // actually on screen.
    expect(document.activeElement).not.toBe(first.element)
    expect(document.activeElement).toBe(wrapper.findAll('.mm-title-btn')[1].element)
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

    const neighbor = wrapper.find('.mm-tile-link')
    expect(neighbor.exists()).toBe(true)
    ;(neighbor.element as HTMLElement).focus()
    await neighbor.trigger('click')
    await nextTick()
    // The panel now shows B, so the link that opened it is gone.
    expect(mm.selectedId).toBe('b')

    await wrapper.find('.pfp-close').trigger('click')
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
        // A person subtype and a log: the Type cell shows 'Colleagues' and
        // 'Logs', which order the opposite way from the raw 'person'/'log'.
        { id: 'mo', title: 'Mo', type: 'person', tags: ['colleague'], aliases: [], description: '', workspace: 'personal', degree: 0, mtime: nowSec, updated: '2026-01-01', stale: false, age_days: 1 },
        { id: 'logbook', title: 'Daily log', type: 'log', tags: [], aliases: [], description: '', workspace: 'personal', degree: 0, mtime: nowSec, updated: '2026-01-01', stale: false, age_days: 2 },
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

  it('orders the Type column by the label its cell renders', async () => {
    // Sorting the raw `type` put 'log' before 'person', so the six person
    // subtypes — which the cell names 'Colleagues', 'Family & partner', … —
    // came out in an order the column never showed.
    const { wrapper } = await mountSortableList()
    await sortButton(wrapper, 'Type').trigger('click')
    await nextTick()

    const order = titles(wrapper)
    expect(order.indexOf('Mo')).toBeLessThan(order.indexOf('Daily log'))
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
 * Section navigation: one level, in the sidebar.
 *
 * The page used to carry a Review/Map switch in its header and a tab bar
 * under it, while Settings listed its sections in the sidebar. The sections
 * are routes now (/memory/<section>), listed in the sidebar; the page itself
 * only names where you are. These pin the routes, the panels each one shows,
 * and the absence of the old header switch and tab row.
 */
describe('MemoryMapView sections', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    apiGet.mockReset()
    apiPost.mockReset()
    apiGet.mockImplementation((url: string) => {
      if (url.includes('/api/vault/graph')) return Promise.resolve(graphPayload())
      if (url.startsWith('/api/memory/entity-types')) {
        return Promise.resolve({
          workspace: 'personal', vault: '/tmp/vault',
          types: [{
            id: 'customer', label: 'Customer', kind: 'entity', folder: 'Customers',
            description: 'Someone we do business with.', aliases: ['client'],
            stale_after_days: 0, enabled: true, builtin: false, note_count: 2,
          }],
        })
      }
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

  async function mountSection(section: string) {
    const wrapper = await mountView(`/memory/${section}`)
    await flushPromises()
    await nextTick()
    await flushPromises()
    return { wrapper, mm: useMemoryMapStore() }
  }

  it('has no tab row and no mode switch; the header names the section', async () => {
    const { wrapper } = await mountSection('review')
    expect(wrapper.findAll('[role="tablist"]')).toHaveLength(0)
    const header = wrapper.get('.pane-header')
    expect(header.find('.memory-mode-actions').exists()).toBe(false)
    expect(header.text()).toContain('Memory · To decide')
    wrapper.unmount()
  })

  it('shows both queues on To decide, with a filter for each', async () => {
    const { wrapper, mm } = await mountSection('review')
    expect([mm.section, mm.reviewFilter]).toEqual(['review', 'all'])
    const chips = wrapper.get('.mm-decide-chips').findAll('.mr-chip')
    expect(chips.map(c => c.text())).toEqual(['All 2', 'Suggested 1', 'To revisit 1'])
    const suggested = wrapper.get('.proposal-review')
    const revisit = wrapper.get('.vault-review')
    expect(suggested.isVisible()).toBe(true)
    expect(revisit.isVisible()).toBe(true)
    expect(suggested.text()).toContain('A queued fact')
    expect(revisit.text()).toContain('Mo')

    await chips[2]!.trigger('click')
    await flushPromises()
    expect(wrapper.router.currentRoute.value.fullPath).toBe('/memory/review?show=revisit')
    expect(mm.reviewFilter).toBe('revisit')
    // Hidden, not unmounted: flipping back must not refetch the queue.
    expect(wrapper.get('.proposal-review').isVisible()).toBe(false)
    expect(wrapper.get('.vault-review').isVisible()).toBe(true)
    wrapper.unmount()
  })

  it('lands a deep link on the queue it names', async () => {
    const { wrapper, mm } = await mountSection('review?show=suggested')
    expect(mm.reviewFilter).toBe('suggested')
    expect(wrapper.get('.vault-review').isVisible()).toBe(false)
    expect(wrapper.get('.proposal-review').isVisible()).toBe(true)
    wrapper.unmount()
  })

  it('reaches the retired notes at their own address', async () => {
    const { wrapper, mm } = await mountSection('retired')
    expect(mm.section).toBe('retired')
    expect(wrapper.text()).toContain('Old')
    wrapper.unmount()
  })

  it('shows the decision ledger on /memory/history', async () => {
    const { wrapper } = await mountSection('history')
    expect(wrapper.text()).toContain('Remember the thing')
    wrapper.unmount()
  })

  it('shows the category list on /memory/categories', async () => {
    const { wrapper, mm } = await mountSection('categories')
    expect(mm.section).toBe('categories')
    expect(wrapper.get('.pane-header').text()).toContain('Memory · Categories')
    // The panel, for the active workspace: one row per effective entry.
    expect(wrapper.findAll('.cat-row')).toHaveLength(1)
    expect(wrapper.text()).toContain('Customer')
    // Configuration, not a queue: the always-loaded budget rail is not beside
    // it, and neither is the map's canvas.
    expect(wrapper.find('.mm-review-rail').exists()).toBe(false)
    expect(wrapper.find('.mm-body').exists()).toBe(false)
    wrapper.unmount()
  })

  it('shows the import panel on /memory/import, as a page and not a card on the map', async () => {
    const { wrapper, mm } = await mountSection('import')
    expect(mm.section).toBe('import')
    expect(wrapper.get('.pane-header').text()).toContain('Memory · Import')
    // Its own scrolling page: no canvas, no map toolbar, no review rail, so a
    // listing of any length scrolls the pane instead of squeezing the map to
    // zero height and pushing the controls below the list out of reach (#1029).
    expect(wrapper.find('.import-sources').exists()).toBe(true)
    expect(wrapper.find('.mm-review-wrap').exists()).toBe(true)
    expect(wrapper.find('.mm-body').exists()).toBe(false)
    expect(wrapper.find('.mm-toolbar').exists()).toBe(false)
    expect(wrapper.find('.mm-review-rail').exists()).toBe(false)
    // Discovery is opt-in: mounting the section scans nothing and previews
    // nothing. An **allowlist**, not a deny-list of two route names — a deny-list
    // lets any later `/api/import/…` GET through unexamined, and a history-reading
    // GET is exactly the thing that must not be added by accident. The one import
    // GET this page does make is Ciaobot's *own* batch store (the run list, so a
    // returning user sees an import they left running instead of an empty list):
    // metadata only, opening no conversation, and pinned in ImportSources.test.ts.
    const importGets = apiGet.mock.calls
      .map(([url]) => String(url))
      .filter((url) => url.startsWith('/api/import/'))
    expect(importGets).toEqual(['/api/import/batches?workspace=personal'])
    // Preview is a POST, so a GET predicate could never have caught it: the scan
    // and the preview are checked on the mock that can actually issue them.
    const importPosts = apiPost.mock.calls.map(([url]) => String(url))
    expect(importPosts.filter((url) => url.startsWith('/api/import/'))).toEqual([])
    wrapper.unmount()

    const map = await mountSection('map')
    expect(map.wrapper.find('.import-sources').exists()).toBe(false)
    map.wrapper.unmount()
  })

  it('follows the route when the sidebar moves to another section', async () => {
    const { wrapper, mm } = await mountSection('review')
    await wrapper.router.push('/memory/map')
    await flushPromises()
    expect(mm.section).toBe('map')
    expect(wrapper.find('.mm-toolbar').exists()).toBe(true)
    expect(wrapper.find('.mm-review-wrap').exists()).toBe(false)
    wrapper.unmount()
  })

  it('sends bare /memory to the section last visited', async () => {
    const mm = useMemoryMapStore()
    mm.setSection('retired')
    const wrapper = await mountView('/memory')
    await flushPromises()
    expect(wrapper.router.currentRoute.value.path).toBe('/memory/retired')
    wrapper.unmount()
  })

  it('names the workspace only in the sidebar scope, not in the review body', async () => {
    const { wrapper } = await mountSection('review')
    const body = wrapper.get('.mm-review-wrap .page-main').text()
    expect(body).not.toContain('to decide in')
    expect(body.toLowerCase()).not.toContain('personal')
    wrapper.unmount()
  })

  it('keeps only the always-loaded budget on the review rail', async () => {
    const guide = '# Guide\n\n<!-- ciao:memory:start -->\nPrefers short briefs\n<!-- ciao:memory:end -->\n'
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const path = new URL(String(input), 'http://localhost').searchParams.get('path')
      return path === 'personal/AGENTS.md'
        ? { ok: true, status: 200, text: async () => guide } as unknown as Response
        : { ok: false, status: 404, text: async () => '' } as unknown as Response
    }))
    // The count uses the viewer's locale (`toLocaleString()`), so pin one here:
    // otherwise the machine's decides, and en-CH renders 3000 as `3'000`.
    const toLocaleString = Number.prototype.toLocaleString
    vi.spyOn(Number.prototype, 'toLocaleString').mockImplementation(
      function (this: number, locales?: Intl.LocalesArgument, options?: Intl.NumberFormatOptions) {
        return toLocaleString.call(this, locales ?? 'en-US', options)
      },
    )
    const { wrapper } = await mountSection('review')
    await flushPromises()

    const rail = wrapper.get('.mm-review-rail')
    // The vault's numbers live on the map's toolbar now.
    expect(rail.find('.rail-kv').exists()).toBe(false)
    expect(rail.get('.guide-budget-path').text()).toBe('personal/AGENTS.md')
    const regions = rail.findAll('.guide-region')
    expect(regions[0]!.get('.guide-region-name').text()).toBe('Agent memory')
    // 20 chars of entry plus the trailing newline, against the 3000 default cap.
    expect(regions[0]!.get('.guide-region-count').text()).toBe('21 / 3,000')
    expect(regions[0]!.get('[role="meter"]').attributes('aria-valuenow')).toBe('21')
    wrapper.unmount()
  })
})

// ── A note somebody has already checked ─────────────────────────────────────
//
// The map's `stale` flag is the age rule, and the managed pass only ever
// CLEARS it. A note in its cooldown, or one a proposal is waiting on, is not
// "unchecked" — the tile has to say which, or the flag's absence looks like the
// note being quietly forgotten.

describe('MemoryMapView verification state', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    apiGet.mockReset()
    apiGet.mockImplementation((url: string) => {
      if (url.includes('/api/vault/graph')) return Promise.resolve(checkedPayload())
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

  async function selectNote(id: string) {
    const wrapper = await mountView('/memory/map')
    await flushPromises()
    await nextTick()
    useMemoryMapStore().selectNode(id)
    await nextTick()
    await flushPromises()
    return wrapper
  }

  it('leads the tile with a pending proposal and a link to it, not "needs review"', async () => {
    const wrapper = await selectNote('pending')

    const lead = wrapper.get('.mm-tile .mm-tile-pending').text()
    expect(lead).toContain('Being checked')
    expect(lead).toContain('2026-09-28')
    expect(wrapper.get('.mm-tile-pending a').text()).toBe('Open the proposal')
    // Being asked about is not being ignored, and saying "unchecked" here would
    // link the operator to a queue that has stopped asking on purpose.
    expect(wrapper.find('.mm-tile-stale').exists()).toBe(false)
    wrapper.unmount()
  })

  it('shows a recorded verdict, its coverage and when the note is due again', async () => {
    const wrapper = await selectNote('checked')

    const lead = wrapper.get('.mm-tile .mm-tile-checked').text()
    expect(lead).toContain('Checked 2026-09-28')
    expect(lead).toContain('the note is still true')
    expect(lead).toContain('covering the whole note')
    expect(lead).toContain('2026-10-28')
    wrapper.unmount()
  })

  it('says a partial check covered part of the note', async () => {
    const wrapper = await selectNote('partial')

    expect(wrapper.get('.mm-tile-checked').text()).toContain('covering part of the note')
    wrapper.unmount()
  })

  it('says a dead proposal means the note is unchecked again', async () => {
    const wrapper = await selectNote('conflicted')

    expect(wrapper.get('.mm-tile-stale').text())
      .toContain('A verification is out of date for this note')
    // Its accept would refuse as a conflict, so the tile must not offer a link
    // to a card nobody can use.
    expect(wrapper.find('.mm-tile-pending').exists()).toBe(false)
    wrapper.unmount()
  })

  it('still calls a never-checked note out', async () => {
    const wrapper = await selectNote('untouched')

    expect(wrapper.get('.mm-tile-stale').text()).toContain('Unchecked for')
    expect(wrapper.find('.mm-tile-checked').exists()).toBe(false)
    expect(wrapper.find('.mm-tile-pending').exists()).toBe(false)
    wrapper.unmount()
  })

  it('counts only the note that is genuinely unchecked', async () => {
    const wrapper = await mountView('/memory/map')
    await flushPromises()
    await nextTick()

    const mm = useMemoryMapStore()
    // Three carry a check that settles their revision and the server cleared
    // their flags, so the toolbar must not re-add them. The fourth is a dead
    // proposal — the note really IS unchecked again — so it belongs here.
    expect(mm.staleNotes.map(n => n.title)).toEqual(['Conflicted', 'Untouched'])
    expect(wrapper.get('.mm-toolbar-stale').text()).toContain('2')
    wrapper.unmount()
  })
})

/**
 * A reloadable proposal link: `/memory/review?show=suggested&workspace=NAME&proposal=prv_<64 hex>`.
 *
 * The real panel is mounted; only layout (jsdom has none), the frame timer and
 * the network are replaced. Fetches are held on deferred promises and the
 * panel's 16 ms frames on fake timers, so each test decides when the queue
 * arrives and when the row becomes visible.
 */
describe('MemoryMapView proposal links', () => {
  type Deferred<T> = { promise: Promise<T>; resolve: (v: T) => void; reject: (e: unknown) => void }
  function deferred<T>(): Deferred<T> {
    let resolve!: (v: T) => void
    let reject!: (e: unknown) => void
    const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej })
    return { promise, resolve, reject }
  }
  const keyOf = (c: string) => `prv_${c.repeat(64)}`
  const linkTo = (c: string, ws = 'personal') =>
    `/memory/review?show=suggested&workspace=${ws}&proposal=${keyOf(c)}`
  const row = (id: string, text: string, c: string, ws = 'personal') => ({
    id, kind: 'memory', text, source: '', path: '', workspace: ws, region: 'memory',
    target: '', ts: '2026-09-01', review_key: keyOf(c), review_path: linkTo(c, ws),
  })
  const QUEUE = () => ({
    rows: [
      row('p1', 'Alpha fact', 'a'),
      row('p2', 'Beta fact', 'b'),
      row('p3', 'Gamma fact', 'c', 'work'),
    ],
  })

  let listing: () => Promise<unknown>
  let visible: boolean
  let focused: string[]
  let current: ReturnType<typeof mount> | null

  beforeEach(() => {
    setActivePinia(createPinia())
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    apiGet.mockReset()
    apiPost.mockReset()
    visible = true
    focused = []
    current = null
    listing = () => Promise.resolve(QUEUE())
    apiGet.mockImplementation((url: string) => {
      if (url === '/api/proposals') return listing()
      if (url.startsWith('/api/proposals/history')) return Promise.resolve({ rows: [], total: 0, truncated: false, limit: 200 })
      if (url.startsWith('/api/vault/review')) return Promise.resolve({ candidates: [], trashed: [], cleared: [] })
      if (url.includes('/api/vault/graph')) return Promise.resolve(graphPayload())
      return Promise.resolve({})
    })
    const store = useProjectStore()
    store.workspaces = [
      { name: 'personal', vault_root: '/tmp/vault', default_provider: 'claude', gws_profile: '' },
      { name: 'work', vault_root: '/tmp/work', default_provider: 'claude', gws_profile: '' },
    ]
    store.activeWorkspace = 'personal'
    store.bootstrapped = true
    vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
    HTMLCanvasElement.prototype.getContext = vi.fn(() => null) as never
    vi.stubGlobal('requestAnimationFrame', vi.fn(() => 1))
    vi.stubGlobal('cancelAnimationFrame', vi.fn())
    // jsdom lays nothing out: `visible` is what the panel's frame loop waits for.
    vi.spyOn(Element.prototype, 'getClientRects').mockImplementation(
      () => (visible ? [{}] : []) as unknown as DOMRectList,
    )
    Element.prototype.scrollIntoView = vi.fn()
    const realFocus = HTMLElement.prototype.focus
    vi.spyOn(HTMLElement.prototype, 'focus').mockImplementation(function (this: HTMLElement, ...args) {
      if (this.classList.contains('pr-row')) focused.push(/Alpha|Beta|Gamma/.exec(this.textContent ?? '')?.[0] ?? '?')
      else realFocus.apply(this, args)
    })
  })

  afterEach(() => {
    current?.unmount()
    document.body.innerHTML = ''
    vi.useRealTimers()
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
    delete (Element.prototype as { scrollIntoView?: unknown }).scrollIntoView
  })

  /** Let promises and the panel's 16 ms frames run a few rounds. */
  async function settle() {
    for (let i = 0; i < 4; i += 1) {
      await flushPromises()
      await vi.advanceTimersByTimeAsync(16)
    }
    await flushPromises()
  }
  async function open(path: string) {
    const wrapper = await mountView(path)
    current = wrapper
    await settle()
    return wrapper
  }
  const noticeText = (wrapper: ReturnType<typeof mount>) => wrapper.find('.mm-link-notice').text()

  it('a fresh mount waits for bootstrap and the deferred fetch, then reveals the exact row and clears saved filters', async () => {
    const proposals = useProposalsStore()
    proposals.kindFilter = 'skill'
    proposals.decideFilter = 'needs-you'
    proposals.search = 'zzz'
    useProjectStore().bootstrapped = false
    const gate = deferred<unknown>()
    listing = () => gate.promise

    const wrapper = await open(linkTo('b'))
    expect(noticeText(wrapper)).toContain('Opening the proposal')
    useProjectStore().bootstrapped = true
    await settle()
    expect(focused).toEqual([])

    gate.resolve(QUEUE())
    await settle()

    expect(focused).toEqual(['Beta'])
    expect([proposals.kindFilter, proposals.decideFilter, proposals.search]).toEqual(['all', 'all', ''])
    expect(proposals.revealedRowId).toBe('')
    expect(wrapper.find('.mm-link-notice').exists()).toBe(false)
    expect(apiPost).not.toHaveBeenCalled()
  })

  it('a same-component route update reveals the exact row after a deferred fetch', async () => {
    const proposals = useProposalsStore()
    const wrapper = await open('/memory/review')
    expect(focused).toEqual([])
    proposals.kindFilter = 'skill'
    proposals.search = 'zzz'
    const gate = deferred<unknown>()
    listing = () => gate.promise

    await wrapper.router.push(linkTo('a'))
    await settle()
    expect(focused).toEqual([])

    gate.resolve(QUEUE())
    await settle()

    expect(focused).toEqual(['Alpha'])
    expect([proposals.kindFilter, proposals.search]).toEqual(['all', ''])
  })

  it('a registered other workspace is entered through the switch guards', async () => {
    const store = useProjectStore()
    const guard = vi.fn(async () => true)
    store.registerWorkspaceSwitchGuard(guard)

    await open(linkTo('c', 'work'))

    expect(guard).toHaveBeenCalledWith('personal')
    expect(store.activeWorkspace).toBe('work')
    expect(focused).toEqual(['Gamma'])
  })

  it('a declined guard leaves the scope and the focus alone', async () => {
    const store = useProjectStore()
    store.registerWorkspaceSwitchGuard(async () => false)

    const wrapper = await open(linkTo('c', 'work'))

    expect(store.activeWorkspace).toBe('personal')
    expect(focused).toEqual([])
    expect(useProposalsStore().revealedRowId).toBe('')
    expect(noticeText(wrapper)).toContain('The workspace was not changed')
  })

  it('an unknown workspace is said so and changes nothing', async () => {
    const store = useProjectStore()
    const wrapper = await open(linkTo('a', 'ghost'))

    expect(store.activeWorkspace).toBe('personal')
    expect(focused).toEqual([])
    expect(noticeText(wrapper)).toContain('"ghost" is not available')
  })

  it('a malformed or repeated key is refused and never picks another row', async () => {
    const short = await open('/memory/review?show=suggested&workspace=personal&proposal=prv_abc')
    expect(noticeText(short)).toContain('not valid')
    short.unmount()
    current = null

    const repeated = await open(`/memory/review?show=suggested&workspace=personal&proposal=${keyOf('a')}&proposal=${keyOf('b')}`)
    expect(noticeText(repeated)).toContain('not valid')
    expect(focused).toEqual([])
    expect(useProjectStore().activeWorkspace).toBe('personal')
  })

  it('a key that belongs to another workspace is not found here', async () => {
    const wrapper = await open(linkTo('c', 'personal'))

    expect(noticeText(wrapper)).toContain('This proposal is no longer pending.')
    expect(focused).toEqual([])
  })

  it('a missing or resolved key says it is no longer pending and links to history', async () => {
    const wrapper = await open(linkTo('f'))

    expect(noticeText(wrapper)).toContain('This proposal is no longer pending.')
    expect(wrapper.get('.mm-link-notice a').attributes('href')).toBe('/memory/history')
    expect(focused).toEqual([])
  })

  it('a transport failure is not a missing-state claim, and Retry succeeds', async () => {
    listing = () => Promise.reject(new Error('offline'))
    const wrapper = await open(linkTo('b'))

    const alert = wrapper.get('.mm-link-notice[role="alert"]')
    expect(alert.text()).toContain('Could not load the proposal')
    expect(wrapper.text()).not.toContain('no longer pending')
    expect(focused).toEqual([])

    listing = () => Promise.resolve(QUEUE())
    await alert.get('button').trigger('click')
    await settle()

    expect(focused).toEqual(['Beta'])
    expect(wrapper.find('.mm-link-notice').exists()).toBe(false)
  })

  it('route A to B while A is still loading: A cannot overwrite B', async () => {
    const proposals = useProposalsStore()
    const reveal = vi.spyOn(proposals, 'revealRow')
    const gate = deferred<unknown>()
    listing = () => gate.promise
    const wrapper = await open(linkTo('a'))

    await wrapper.router.push(linkTo('b'))
    await settle()
    gate.resolve(QUEUE())
    await settle()

    expect(reveal.mock.calls.map(c => c[0])).toEqual(['p2'])
    expect(focused).toEqual(['Beta'])
  })

  it('after handoff, navigating to B never lets A focus or clear B', async () => {
    const proposals = useProposalsStore()
    visible = false
    const wrapper = await open(linkTo('a'))
    expect(proposals.revealedRowId).toBe('p1')

    await wrapper.router.push(linkTo('b'))
    await settle()
    expect(proposals.revealedRowId).toBe('p2')
    expect(focused).toEqual([])

    visible = true
    await settle()
    expect(focused).toEqual(['Beta'])
    expect(proposals.revealedRowId).toBe('')
  })

  it('after handoff, a workspace switch the resolver did not make withdraws the reveal', async () => {
    const proposals = useProposalsStore()
    visible = false
    await open(linkTo('a'))
    expect(proposals.revealedRowId).toBe('p1')

    useProjectStore().activeWorkspace = 'work'
    await settle()
    expect(proposals.revealedRowId).toBe('')

    visible = true
    await settle()
    expect(focused).toEqual([])
  })

  it('after handoff, unmounting stops the old loop and withdraws the reveal', async () => {
    const proposals = useProposalsStore()
    visible = false
    const wrapper = await open(linkTo('a'))
    expect(proposals.revealedRowId).toBe('p1')

    wrapper.unmount()
    current = null
    expect(proposals.revealedRowId).toBe('')

    visible = true
    await settle()
    expect(focused).toEqual([])
  })

  it('exactly equivalent duplicates reveal the first, and no decision is ever sent', async () => {
    listing = () => Promise.resolve({
      rows: [row('p1', 'Alpha fact', 'a'), row('p9', 'Beta fact', 'a'), row('p2', 'Gamma fact', 'b')],
    })

    await open(linkTo('a'))

    expect(focused).toEqual(['Alpha'])
    expect(apiPost).not.toHaveBeenCalled()
  })

  it('without a proposal query, or outside To decide, nothing is resolved or changed', async () => {
    const proposals = useProposalsStore()
    const reveal = vi.spyOn(proposals, 'revealRow')
    proposals.kindFilter = 'skill'

    const plain = await open('/memory/review')
    expect(plain.find('.mm-link-notice').exists()).toBe(false)
    expect(proposals.kindFilter).toBe('skill')
    plain.unmount()
    current = null

    const map = await open(`/memory/map?workspace=personal&proposal=${keyOf('a')}`)
    expect(map.find('.mm-link-notice').exists()).toBe(false)
    expect(reveal).not.toHaveBeenCalled()
    expect(proposals.kindFilter).toBe('skill')
  })

  it('a Vault review reveal of the same row survives the route change that follows a finished link', async () => {
    const proposals = useProposalsStore()
    const wrapper = await open(linkTo('a'))
    expect(focused).toEqual(['Alpha'])

    // What VaultReviewPanel does: reveal the row, then go to the queue.
    visible = false
    proposals.revealRow('p1')
    await wrapper.router.push('/memory/review?show=suggested')
    await settle()

    expect(proposals.revealedRowId).toBe('p1')
  })

  it('a manual workspace switch after a finished link is not resolver work', async () => {
    const proposals = useProposalsStore()
    await open(linkTo('a'))
    expect(focused).toEqual(['Alpha'])

    visible = false
    proposals.revealRow('p1')
    await settle()
    useProjectStore().activeWorkspace = 'work'
    await settle()

    // Not the resolver's reveal, so the resolver must not take it back.
    expect(proposals.revealedRowId).toBe('p1')
  })

  it('Retry leaves keyboard focus inside the notice', async () => {
    listing = () => Promise.reject(new Error('offline'))
    const wrapper = await open(linkTo('b'))
    const gate = deferred<unknown>()
    listing = () => gate.promise

    const button = wrapper.get('.mm-link-notice button').element as HTMLButtonElement
    button.focus()
    expect(document.activeElement).toBe(button)
    await wrapper.get('.mm-link-notice button').trigger('click')
    await settle()

    const notice = wrapper.get('.mm-link-notice').element
    expect(notice.contains(document.activeElement)).toBe(true)
    expect(document.activeElement).not.toBe(document.body)
    gate.resolve(QUEUE())
    await settle()
  })

  it('a forced fetch superseded by a newer request neither reveals nor claims the row is gone', async () => {
    const proposals = useProposalsStore()
    listing = () => Promise.reject(new Error('offline'))
    const wrapper = await open(linkTo('b'))
    const stale = deferred<unknown>()
    const newer = deferred<unknown>()
    const gates = [stale, newer]
    listing = () => gates.shift()!.promise

    await wrapper.get('.mm-link-notice button').trigger('click')
    await settle()
    void proposals.fetch({ force: true })
    await settle()

    // The superseded request answers first, with a queue that lacks the row.
    stale.resolve({ rows: [] })
    await settle()
    expect(focused).toEqual([])
    expect(wrapper.text()).not.toContain('no longer pending')

    newer.resolve(QUEUE())
    await settle()
    expect(focused).toEqual(['Beta'])
  })

  it('a manual workspace switch while the resolver is switching stops it and keeps the reader\'s scope', async () => {
    const store = useProjectStore()
    store.workspaces = [...store.workspaces, { name: 'third', vault_root: '/tmp/third', default_provider: 'claude', gws_profile: '' }]
    const release = deferred<boolean>()
    store.registerWorkspaceSwitchGuard(() => release.promise)
    await open(linkTo('c', 'work'))

    store.activeWorkspace = 'third'
    await settle()
    release.resolve(true)
    await settle()

    expect(store.activeWorkspace).toBe('third')
    expect(focused).toEqual([])
    expect(useProposalsStore().revealedRowId).toBe('')
  })
})
