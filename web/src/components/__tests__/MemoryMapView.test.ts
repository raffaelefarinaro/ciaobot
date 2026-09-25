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

const apiGet = vi.hoisted(() => vi.fn())

vi.mock('../../lib/api', () => ({
  api: { get: apiGet, post: vi.fn(), patch: vi.fn(), del: vi.fn() },
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

  async function mountSection(section: string) {
    const wrapper = await mountView(`/memory/${section}`)
    await flushPromises()
    await nextTick()
    await flushPromises()
    return { wrapper, mm: useMemoryMapStore() }
  }

  it('has no tab row and no mode switch; the header names the section', async () => {
    const { wrapper } = await mountSection('revisit')
    expect(wrapper.findAll('[role="tablist"]')).toHaveLength(0)
    const header = wrapper.get('.pane-header')
    expect(header.find('.memory-mode-actions').exists()).toBe(false)
    expect(header.text()).toContain('Memory · To revisit')
    wrapper.unmount()
  })

  it('shows the notes to revisit on /memory/revisit', async () => {
    const { wrapper, mm } = await mountSection('revisit')
    expect([mm.section, mm.reviewTab, mm.retirementTab]).toEqual(['revisit', 'retirement', 'candidates'])
    expect(wrapper.text()).toContain('Mo')
    wrapper.unmount()
  })

  it('reaches the retired notes at their own address', async () => {
    const { wrapper, mm } = await mountSection('retired')
    expect(mm.retirementTab).toBe('trash')
    expect(wrapper.text()).toContain('Old')
    wrapper.unmount()
  })

  it('shows the decision ledger on /memory/history', async () => {
    const { wrapper } = await mountSection('history')
    expect(wrapper.text()).toContain('Remember the thing')
    wrapper.unmount()
  })

  it('follows the route when the sidebar moves to another section', async () => {
    const { wrapper, mm } = await mountSection('suggested')
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
    const { wrapper } = await mountSection('suggested')
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
    const { wrapper } = await mountSection('suggested')
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
