// @vitest-environment jsdom

/**
 * The workspace guide card reads `<workspace>/CLAUDE.md`.
 *
 * A bare `CLAUDE.md` would let /api/workspace-file's fuzzy lookup resolve to
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
  })

  afterEach(() => {
    document.body.innerHTML = ''
    vi.unstubAllGlobals()
    vi.restoreAllMocks()
  })

  it('asks for the active workspace guide before any bare basename', async () => {
    const fetchMock = stubWorkspaceFile({ 'work/CLAUDE.md': guideFile('A note') })
    const wrapper = await mountSidebar()

    expect(guidePaths(fetchMock)[0]).toBe('work/CLAUDE.md')
    expect(guidePaths(fetchMock)).not.toContain('CLAUDE.md')
    expect(wrapper.find('.guide-card-title').text()).toContain('work/CLAUDE.md')
  })

  it('re-fetches under the new workspace when the active one changes', async () => {
    const fetchMock = stubWorkspaceFile({
      'work/CLAUDE.md': guideFile('Work note'),
      'personal/CLAUDE.md': guideFile('Personal note'),
    })
    const wrapper = await mountSidebar()
    const store = useProjectStore()
    store.activeWorkspace = 'personal'
    await flushPromises()

    expect(guidePaths(fetchMock)).toContain('personal/CLAUDE.md')
    expect(wrapper.find('.guide-card-title').text()).toContain('personal/CLAUDE.md')
  })

  it('falls back to the bare basename only when the qualified path is missing', async () => {
    const fetchMock = stubWorkspaceFile({ 'CLAUDE.md': guideFile('Root note') })
    const wrapper = await mountSidebar()

    expect(guidePaths(fetchMock).slice(0, 2)).toEqual(['work/CLAUDE.md', 'work/AGENTS.md'])
    expect(wrapper.find('.guide-card-title').text()).toContain('CLAUDE.md')
  })

  it('counts an impossible calendar date as malformed, not valid', async () => {
    stubWorkspaceFile({ 'work/CLAUDE.md': guideFile('Expires soon [expires: 2026-02-30]') })
    const wrapper = await mountSidebar()

    expect(wrapper.find('.guide-card-regions').text()).toContain('1 malformed tag')
  })

  it('accepts a real calendar date', async () => {
    stubWorkspaceFile({ 'work/CLAUDE.md': guideFile('Expires soon [expires: 2026-02-28]') })
    const wrapper = await mountSidebar()

    expect(wrapper.find('.guide-card-regions').text()).not.toContain('malformed')
  })
})
