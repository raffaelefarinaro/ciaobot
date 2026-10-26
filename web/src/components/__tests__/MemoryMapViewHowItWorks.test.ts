// @vitest-environment jsdom

/**
 * The "How memory works" disclosure is mounted once, under the header, so it
 * shows on every Memory section and no panel carries its own copy.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { flushPromises, mount } from '@vue/test-utils'
import MemoryMapView from '../MemoryMapView.vue'
import { useProjectStore } from '../../stores/projects'

const apiGet = vi.hoisted(() => vi.fn())

vi.mock('../../lib/api', () => ({
  api: { get: apiGet, post: vi.fn(), patch: vi.fn(), del: vi.fn() },
}))

const Stub = { template: '<div />' }
const Panel = { template: '<div class="panel-stub" />' }

async function mountAt(path: string) {
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
    global: {
      plugins: [router],
      stubs: {
        MemoryCategoriesPanel: Panel,
        ProposalReviewPanel: Panel,
        VaultReviewPanel: Panel,
        ImportSources: Panel,
        MemoryGuideBudget: Panel,
        PinnedFilePanel: Panel,
      },
    },
  })
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  setActivePinia(createPinia())
  localStorage.clear()
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
  HTMLCanvasElement.prototype.getContext = vi.fn(() => null) as never
  apiGet.mockReset()
  apiGet.mockResolvedValue({ types: [], nodes: [], edges: [], proposals: [], items: [] })
  useProjectStore()
})

afterEach(() => {
  document.body.innerHTML = ''
})

describe('MemoryMapView How memory works disclosure', () => {
  for (const section of ['review', 'map', 'categories', 'import', 'retired', 'history']) {
    it(`shows exactly one disclosure on /memory/${section}`, async () => {
      const wrapper = await mountAt(`/memory/${section}`)
      const toggles = wrapper.findAll('.mhw-toggle')

      expect(toggles).toHaveLength(1)
      expect(toggles[0].text()).toBe('How memory works')
    })
  }
})
