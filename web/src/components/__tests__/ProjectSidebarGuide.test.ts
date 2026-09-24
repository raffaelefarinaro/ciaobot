// @vitest-environment jsdom

/**
 * The workspace guide card reads `<workspace>/AGENTS.md`.
 *
 * A bare `AGENTS.md` would let /api/workspace-file's fuzzy lookup resolve to
 * whichever workspace sorts first, so the card — and its Open/Discuss actions —
 * could expose another workspace's instructions.
 *
 * Its health stats must also reject impossible calendar dates the way the
 * backend validator (`ciao/memory_tool.expiration_tag_error`) does: `new Date`
 * rolls 2026-02-30 over to March 2 instead of failing.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { flushPromises, mount } from '@vue/test-utils'
import ProjectSidebar from '../ProjectSidebar.vue'
import { useProjectStore } from '../../stores/projects'
import { useMemoryMapStore } from '../../stores/memoryMap'

vi.mock('../../lib/api', () => ({
  api: { get: vi.fn().mockResolvedValue({ rows: [] }), post: vi.fn(), patch: vi.fn(), del: vi.fn() },
}))

function guideFile(body: string): string {
  return `# Guide\n\n<!-- ciao:memory:start -->\n${body}\n<!-- ciao:memory:end -->\n`
}

/** Serve `content` for `path` only; every other path 404s. */
function stubWorkspaceFile(served: Record<string, string>) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    const raw = new URL(url, 'http://localhost').searchParams.get('path') || ''
    if (raw in served) {
      return { ok: true, status: 200, text: async () => served[raw] } as unknown as Response
    }
    return { ok: false, status: 404, text: async () => '' } as unknown as Response
  })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

function guidePaths(fetchMock: ReturnType<typeof vi.fn>): string[] {
  return fetchMock.mock.calls
    .map(c => String(c[0]))
    .filter(u => u.startsWith('/api/workspace-file'))
    .map(u => new URL(u, 'http://localhost').searchParams.get('path') || '')
}

async function mountSidebar() {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/', component: { template: '<div />' } }],
  })
  await router.push('/')
  await router.isReady()
  const wrapper = mount(ProjectSidebar, {
    attachTo: document.body,
    props: { collapsed: false, mode: 'memory' },
    global: { plugins: [router] },
  })
  await flushPromises()
  return wrapper
}

describe('ProjectSidebar workspace guide card', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    const store = useProjectStore()
    store.workspaces = [
      { name: 'personal', vault_root: '/tmp/p', default_provider: 'claude', gws_profile: '' },
      { name: 'work', vault_root: '/tmp/w', default_provider: 'claude', gws_profile: '' },
    ]
    store.activeWorkspace = 'work'
    store.projects = []
    store.chats = []
    // The sidebar follows the main pane's Memory mode; the guide belongs to
    // the graph/map context, not the Review queue.
    useMemoryMapStore().view = 'graph'
  })

  afterEach(() => {
    document.body.innerHTML = ''
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('asks for the active workspace guide before any bare basename', async () => {
    const fetchMock = stubWorkspaceFile({ 'work/AGENTS.md': guideFile('A note') })
    const wrapper = await mountSidebar()
    if (wrapper.get('.mm-memory-details-toggle').attributes('aria-expanded') === 'false') {
      await wrapper.get('.mm-memory-details-toggle').trigger('click')
    }

    expect(guidePaths(fetchMock)[0]).toBe('work/AGENTS.md')
    expect(guidePaths(fetchMock)).not.toContain('AGENTS.md')
    expect(wrapper.find('.guide-card-title').text()).toContain('work/AGENTS.md')
  })

  it('still finds a pre-migration CLAUDE.md, after AGENTS.md', async () => {
    const fetchMock = stubWorkspaceFile({ 'work/CLAUDE.md': guideFile('A note') })
    const wrapper = await mountSidebar()
    if (wrapper.get('.mm-memory-details-toggle').attributes('aria-expanded') === 'false') {
      await wrapper.get('.mm-memory-details-toggle').trigger('click')
    }

    // AGENTS.md is asked for first and 404s; the legacy name still resolves,
    // so an install that has not run the guide migration keeps its card.
    expect(guidePaths(fetchMock).slice(0, 2)).toEqual(['work/AGENTS.md', 'work/CLAUDE.md'])
    expect(wrapper.find('.guide-card-title').text()).toContain('work/CLAUDE.md')
  })

  it('keeps probing when a candidate fails with something other than 404', async () => {
    // Regression: any non-404 used to break the loop, so a transient 503 on
    // the first name (the engine restarting) blanked the card even though the
    // next name would have served it.
    const fetchMock = stubWorkspaceFile({ 'work/CLAUDE.md': guideFile('A note') })
    const original = fetchMock.getMockImplementation()!
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      if (String(input).includes(encodeURIComponent('work/AGENTS.md'))) {
        return { ok: false, status: 503, text: async () => '' } as unknown as Response
      }
      return original(input)
    })
    const wrapper = await mountSidebar()
    if (wrapper.get('.mm-memory-details-toggle').attributes('aria-expanded') === 'false') {
      await wrapper.get('.mm-memory-details-toggle').trigger('click')
    }

    expect(wrapper.find('.guide-card-title').text()).toContain('work/CLAUDE.md')
    expect(wrapper.text()).not.toContain('HTTP 503')
  })

  it('re-fetches under the new workspace when the active one changes', async () => {
    const fetchMock = stubWorkspaceFile({
      'work/AGENTS.md': guideFile('Work note'),
      'personal/AGENTS.md': guideFile('Personal note'),
    })
    const wrapper = await mountSidebar()
    if (wrapper.get('.mm-memory-details-toggle').attributes('aria-expanded') === 'false') {
      await wrapper.get('.mm-memory-details-toggle').trigger('click')
    }
    const store = useProjectStore()
    store.activeWorkspace = 'personal'
    await flushPromises()

    expect(guidePaths(fetchMock)).toContain('personal/AGENTS.md')
    expect(wrapper.find('.guide-card-title').text()).toContain('personal/AGENTS.md')
  })

  it('falls back to the bare basename only when the qualified path is missing', async () => {
    const fetchMock = stubWorkspaceFile({ 'AGENTS.md': guideFile('Root note') })
    const wrapper = await mountSidebar()
    if (wrapper.get('.mm-memory-details-toggle').attributes('aria-expanded') === 'false') {
      await wrapper.get('.mm-memory-details-toggle').trigger('click')
    }

    expect(guidePaths(fetchMock).slice(0, 2)).toEqual(['work/AGENTS.md', 'work/CLAUDE.md'])
    expect(wrapper.find('.guide-card-title').text()).toContain('AGENTS.md')
  })

  it('counts an impossible calendar date as malformed, not valid', async () => {
    stubWorkspaceFile({ 'work/AGENTS.md': guideFile('Expires soon [expires: 2026-02-30]') })
    const wrapper = await mountSidebar()
    if (wrapper.get('.mm-memory-details-toggle').attributes('aria-expanded') === 'false') {
      await wrapper.get('.mm-memory-details-toggle').trigger('click')
    }

    expect(wrapper.find('.guide-card-regions').text()).toContain('1 malformed tag')
  })

  it('accepts a real calendar date', async () => {
    stubWorkspaceFile({ 'work/AGENTS.md': guideFile('Expires soon [expires: 2026-02-28]') })
    const wrapper = await mountSidebar()
    if (wrapper.get('.mm-memory-details-toggle').attributes('aria-expanded') === 'false') {
      await wrapper.get('.mm-memory-details-toggle').trigger('click')
    }

    expect(wrapper.find('.guide-card-regions').text()).not.toContain('malformed')
  })

  it('never falls back to another workspace\'s guide after an error', async () => {
    // Regression: a bare basename fuzzy-resolves to the primary root, so
    // continuing past a transient error on the qualified probes could render
    // `work/AGENTS.md` as `personal`'s guide, with Open/Discuss acting on it.
    const fetchMock = stubWorkspaceFile({ 'AGENTS.md': guideFile("another workspace") })
    const original = fetchMock.getMockImplementation()!
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input)
      if (url.includes(encodeURIComponent('work/'))) {
        return { ok: false, status: 503, text: async () => '' } as unknown as Response
      }
      return original(input)
    })
    const wrapper = await mountSidebar()
    if (wrapper.get('.mm-memory-details-toggle').attributes('aria-expanded') === 'false') {
      await wrapper.get('.mm-memory-details-toggle').trigger('click')
    }

    expect(wrapper.text()).not.toContain('another workspace')
    expect(guidePaths(fetchMock)).not.toContain('AGENTS.md')
  })
})
