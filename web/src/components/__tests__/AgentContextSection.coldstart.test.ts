// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, RouterLinkStub } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import AgentContextSection from '../AgentContextSection.vue'
import type { ProjectInfo } from '../../lib/types'

const PROJECT = {
  project_id: 'p1', name: 'Launch', workspace: 'work',
  context: 'Public beta launch planning.',
} as unknown as ProjectInfo
const PROMPT = 'You are Ciao.\n'.repeat(400)
const META = '.agent-context-summary .agent-context-meta'

describe('AgentContextSection engine cold start', () => {
  let up: boolean
  let guideStatus: number
  let fetchMock: ReturnType<typeof vi.fn>

  beforeEach(() => {
    vi.useFakeTimers()
    setActivePinia(createPinia())
    up = false
    guideStatus = 200
    fetchMock = vi.fn(async (url: string) => {
      if (!up) return new Response('', { status: 503 })
      if (url === '/api/system-prompt') return new Response(JSON.stringify({ text: PROMPT }), { status: 200 })
      return url.includes(encodeURIComponent('work/AGENTS.md')) && guideStatus === 200
        ? new Response('x'.repeat(4000), { status: 200 }) : new Response('', { status: 404 })
    })
    vi.stubGlobal('fetch', fetchMock)
  })
  afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })

  async function mountRail() {
    const wrapper = mount(AgentContextSection, {
      props: { project: PROJECT, contextPct: null, compacting: false, compactDisabled: false },
      global: { stubs: { RouterLink: RouterLinkStub } },
    })
    await flushPromises()
    return wrapper
  }
  const advance = async (ms: number) => { await vi.advanceTimersByTimeAsync(ms); await flushPromises() }

  it('fills itself in once the engine answers, with no click', async () => {
    const wrapper = await mountRail()
    up = true
    await advance(1100)
    expect(wrapper.get(META).text()).toBe('2.4k tokens')
  })

  it('shows no partial total while a part has failed, and offers Retry on the prompt row', async () => {
    const wrapper = await mountRail()
    expect(wrapper.find(META).exists()).toBe(false)
    const row = wrapper.get('.agent-context-system-error')
    expect(row.text()).toContain('Ciaobot system prompt')
    expect(row.text()).toContain('Could not be read just now')
    expect(row.find('button').text()).toBe('Retry')
  })

  it('recovers the system prompt when its Retry button is clicked', async () => {
    const wrapper = await mountRail()
    up = true
    await wrapper.get('.agent-context-system-error button').trigger('click')
    await flushPromises()
    expect(wrapper.find('.agent-context-system-error').exists()).toBe(false)
    await advance(1100) // the guide's own retry
    expect(wrapper.get(META).text()).toBe('2.4k tokens')
  })

  it('stops after four retries', async () => {
    await mountRail()
    const initial = fetchMock.mock.calls.length
    await advance(1000 + 2000 + 4000 + 8000 + 1000)
    const afterRetries = fetchMock.mock.calls.length
    expect(afterRetries).toBeGreaterThan(initial)
    await advance(60000)
    expect(fetchMock.mock.calls.length).toBe(afterRetries)
  })

  it('makes no fetch after unmount', async () => {
    const wrapper = await mountRail()
    const before = fetchMock.mock.calls.length
    wrapper.unmount()
    await advance(30000)
    expect(fetchMock.mock.calls.length).toBe(before)
  })

  it('does not retry a guide that is simply missing (404), and still shows the total', async () => {
    up = true
    guideStatus = 404
    const wrapper = await mountRail()
    const calls = fetchMock.mock.calls.length
    expect(wrapper.find('.agent-context-error').exists()).toBe(false)
    expect(wrapper.get(META).text()).toMatch(/tokens$/)
    await advance(20000)
    expect(fetchMock.mock.calls.length).toBe(calls)
  })
})
