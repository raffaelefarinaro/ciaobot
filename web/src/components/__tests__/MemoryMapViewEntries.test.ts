// @vitest-environment jsdom

/**
 * The facts inside a note, on the Memory Map.
 *
 * A note is not the unit anybody keeps current, so a map that could only answer
 * "is this file old?" would show a person note re-stamped yesterday as clean
 * while it held a fact from 2019. What is pinned here is that the node reports
 * the entry coverage *beside* the whole-note flag (never raising it, because the
 * nightly worklist is where an overdue bullet becomes a plan), that a mixed note
 * looks mixed, and that a node whose body could not be read carries no coverage
 * rather than a clean-looking zero.
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
const PinnedFilePanelStub = {
  props: ['filePath', 'closeLabel', 'inMemoryMap'],
  emits: ['close'],
  template: `<div class="pfp-stub" :data-file="filePath">
    <button type="button" class="pfp-close" :aria-label="closeLabel" @click="$emit('close')">x</button>
    <slot name="lead" /><slot name="after" />
  </div>`,
}

/** One overdue fact, named the way the server reports it. */
function finding(over: Record<string, unknown> = {}) {
  return {
    identity: 'a'.repeat(64),
    excerpt: '- Landlord is Mr Silva [verified: 2019-05-01]',
    reason: 'aged',
    detail: 'unverified for 2698d against a 90d horizon',
    age_days: 2698,
    last_verified: '2019-05-01',
    own_date: true,
    ...over,
  }
}

function payload() {
  const base = (over: Record<string, unknown> = {}) => ({
    tags: [], aliases: [], description: '', workspace: 'personal', degree: 1,
    mtime: 1, updated: '2026-09-29', age_days: 1, threshold_days: 90, ...over,
  })
  return {
    nodes: [
      base({
        id: 'mixed', title: 'Alice', type: 'person',
        // The file is current — the whole-note flag is off — and it still holds a
        // fact from 2019. This is the case the level exists for.
        stale: false,
        entry_coverage: {
          entries: 2, checked: 2, exempt: 0, unverified: 0, uncovered: 0, stale: 1,
          coverage_ratio: 0.8, fully_verified: false,
          stale_entries: [finding()], more_stale_entries: 0,
        },
      }),
      base({
        id: 'clean', title: 'Clean', type: 'person', stale: false,
        entry_coverage: {
          entries: 2, checked: 2, exempt: 0, unverified: 0, uncovered: 0, stale: 0,
          coverage_ratio: 0.9, fully_verified: true, stale_entries: [], more_stale_entries: 0,
        },
      }),
      base({
        id: 'prose', title: 'Prose', type: 'person', stale: false,
        entry_coverage: {
          entries: 0, checked: 0, exempt: 0, unverified: 0, uncovered: 1, stale: 0,
          coverage_ratio: 0.1, fully_verified: false, stale_entries: [], more_stale_entries: 0,
        },
      }),
      base({
        id: 'unreadable', title: 'Unreadable', type: 'person', stale: false,
        entry_coverage: null,
      }),
    ],
    edges: [],
  }
}

async function mountView() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: Stub },
      { path: '/memory/:section?', component: Stub },
    ],
  })
  await router.push('/memory/map')
  await router.isReady()
  const wrapper = mount(MemoryMapView, {
    attachTo: document.body,
    global: { plugins: [router], stubs: { PinnedFilePanel: PinnedFilePanelStub } },
  })
  return wrapper
}

describe('MemoryMapView entry coverage', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    apiGet.mockReset()
    apiGet.mockImplementation((url: string) => {
      if (url.includes('/api/vault/graph')) return Promise.resolve(payload())
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

  async function open(id: string) {
    const wrapper = await mountView()
    await flushPromises()
    const mm = useMemoryMapStore()
    mm.selectedId = id
    await nextTick()
    return { wrapper, mm }
  }

  it('names the overdue fact on a node whose own date is current', async () => {
    const { wrapper } = await open('mixed')
    const block = wrapper.get('.mm-tile-entries')
    expect(block.text()).toContain('Facts inside this note')
    expect(block.text()).toContain('1 past due')
    expect(block.text()).toContain('Landlord is Mr Silva')
    // The date and the number in one clause. A trailing em dash here read as a
    // sentence the layout had cut off, which is a different claim from the one
    // the tile is making.
    expect(block.text()).toContain('Checked 2019-05-01 — unverified for 2698d against a 90d horizon')
    // The whole-note flag is untouched: `stale` is the file verdict and the
    // nightly worklist is where an overdue bullet becomes a plan, so the map
    // reports beside it rather than raising it.
    expect(wrapper.find('.mm-tile-stale').exists()).toBe(false)
    expect(block.classes()).toContain('mm-tile-entries--due')
    wrapper.unmount()
  })

  it('distinguishes a fact nobody checked from one that is merely old', async () => {
    const { wrapper, mm } = await open('mixed')
    mm.selectedNode!.entryCoverage = {
      ...mm.selectedNode!.entryCoverage!,
      stale: 0, unverified: 1,
      stale_entries: [finding({
        reason: 'no-stamp', own_date: false, last_verified: '2026-09-29', age_days: 1,
        detail: 'nobody has recorded a [verified:] check on this entry',
        excerpt: '- Works at Radix',
      })],
    }
    await nextTick()
    const why = wrapper.get('.mm-tile-entry-why').text()
    expect(why).toContain('Never checked')
    // "Last checked <today>" with no qualifier would be a claim about the fact
    // that was never made, so the map says whose date that is instead.
    expect(why).not.toContain('Last checked 2026-09-29')
    wrapper.unmount()
  })

  it('says every fact in the note is current when they all are', async () => {
    const { wrapper } = await open('clean')
    const block = wrapper.get('.mm-tile-entries')
    expect(block.text()).toContain('all of the note read as facts')
    // Neutral, not a warning: a box that shouted about a well-kept note would be
    // ignored on the notes that need it.
    expect(block.classes()).not.toContain('mm-tile-entries--due')
    wrapper.unmount()
  })

  it('says plainly when the note has nothing written as a list item', async () => {
    const { wrapper } = await open('prose')
    const block = wrapper.get('.mm-tile-entries')
    expect(block.text()).toContain('no facts written as list items')
    expect(block.text()).toContain('1 block of prose this check could not read')
    wrapper.unmount()
  })

  it('shows nothing at all for a node whose body could not be read', async () => {
    // A default object of zeroes would read as "read nothing, found nothing
    // wrong" — a claim about the note rather than about this client's ability to
    // see it, which is the one thing a coverage figure must never be.
    const { wrapper } = await open('unreadable')
    expect(wrapper.find('.mm-tile-entries').exists()).toBe(false)
    wrapper.unmount()
  })

  it('counts notes holding an overdue fact separately from unchecked notes', async () => {
    const wrapper = await mountView()
    await flushPromises()
    // The two figures are not a subset relation — a note re-stamped last week is
    // absent from the unchecked list and belongs in this one — so merging them
    // would hide exactly the case this level exists to surface.
    expect(wrapper.find('.mm-toolbar-stale').exists()).toBe(false)
    const chip = wrapper.get('.mm-toolbar-entries')
    expect(chip.text()).toContain('1')
    expect(chip.text()).toContain('with facts unchecked')
    const mm = useMemoryMapStore()
    expect(mm.entryStaleNotes.map(n => n.id)).toEqual(['mixed'])
    wrapper.unmount()
  })
})
