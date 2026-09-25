// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'
import { flushPromises, mount } from '@vue/test-utils'
import SubagentChatView from '../SubagentChatView.vue'
import { useProjectStore } from '../../stores/projects'
import type { SubagentTranscript } from '../../lib/types'

const CHAT_ID = 'chat-1'

function transcript(extra: Partial<SubagentTranscript> = {}): SubagentTranscript {
  return {
    agent_id: 'a1b2c3d4',
    description: 'Sweep the callers',
    subagent_type: 'Explore',
    status: 'completed',
    // Both provider renderers omit `timestamp` on subagent messages, so the
    // fixtures do too — the view must not depend on it.
    messages: [
      { role: 'user', content: 'Find every caller of foo()' },
      { role: 'assistant', content: '_activity line', tool_name: '_activity' },
      { role: 'assistant', content: 'Found **three** callers.' },
    ] as unknown as SubagentTranscript['messages'],
    ...extra,
  }
}

async function mountView(agentId = 'a1b2c3d4') {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { template: '<div />' } },
      { path: '/chat/:chatId', component: { template: '<div />' } },
    ],
  })
  await router.push('/')
  await router.isReady()
  const wrapper = mount(SubagentChatView, {
    props: { chatId: CHAT_ID, agentId },
    global: { plugins: [router], stubs: { PaneHeader: { template: '<header><slot name="title" /><slot name="actions" /></header>' } } },
  })
  await flushPromises()
  return wrapper
}

describe('SubagentChatView', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    const store = useProjectStore()
    vi.spyOn(store, 'loadSubagents').mockResolvedValue()
    store.chats = [
      { chat_id: CHAT_ID, project_id: 'p1', title: 'Refactor the store' },
    ] as unknown as typeof store.chats
    store.subagents = { [CHAT_ID]: [transcript()] }
  })

  afterEach(() => {
    document.body.innerHTML = ''
    vi.restoreAllMocks()
  })

  it('renders the transcript with a read-only note instead of a composer', async () => {
    const wrapper = await mountView()

    expect(wrapper.text()).toContain('Sweep the callers')
    expect(wrapper.text()).toContain('Explore')
    // User bubble, assistant bubble, and the activity rollup rendered as a
    // rollup rather than a bubble.
    expect(wrapper.findAll('.bubble.user')).toHaveLength(1)
    expect(wrapper.findAll('.bubble.assistant')).toHaveLength(1)
    expect(wrapper.findAll('.sub-activity')).toHaveLength(1)
    // A subagent transcript is a record, not a session you can steer.
    expect(wrapper.find('textarea').exists()).toBe(false)
    expect(wrapper.get('.readonly-note').text()).toContain('Replies go to the parent chat.')
    expect(wrapper.get('.ro-tag').text()).toBe('Read only')
    wrapper.unmount()
  })

  it('links back to the chat that spawned it', async () => {
    const wrapper = await mountView()

    const hrefs = wrapper.findAll('a').map(a => a.attributes('href'))
    expect(hrefs).toContain(`/chat/${CHAT_ID}`)
    expect(wrapper.text()).toContain('Refactor the store')
    expect(wrapper.get('.back-btn').attributes('aria-label')).toBe('Back to Refactor the store')
    expect(wrapper.get('.agent-name').text()).toBe('Sweep the callers')
    wrapper.unmount()
  })

  // The prefixed spelling comes from the local-JSONL fallback; the route
  // always carries the bare id, so the two must resolve to one transcript.
  it('matches an agent id whichever way it is spelled', async () => {
    const store = useProjectStore()
    store.subagents = { [CHAT_ID]: [transcript({ agent_id: 'agent-a1b2c3d4' })] }

    const wrapper = await mountView('a1b2c3d4')

    expect(wrapper.text()).toContain('Sweep the callers')
    wrapper.unmount()
  })

  it('says so plainly when the transcript is not on this machine', async () => {
    const store = useProjectStore()
    store.subagents = { [CHAT_ID]: [] }

    const wrapper = await mountView('missing-agent')

    expect(wrapper.text()).toContain('not available on this machine')
    expect(wrapper.get('#subagent-empty-title').text()).toBe('Transcript not on this machine')
    expect(wrapper.get('.subagent-empty-link').attributes('href')).toBe(`/chat/${CHAT_ID}`)
    wrapper.unmount()
  })

  // The sidebar row disappears when the agent finishes, so the view has to
  // keep reporting live state on its own while it is open.
  it('shows a running status while the sidebar still lists the agent', async () => {
    const store = useProjectStore()
    store.runningSubagents = {
      [CHAT_ID]: [{ agent_id: 'a1b2c3d4', status: 'running' }],
    }

    const wrapper = await mountView()

    expect(wrapper.get('.status-value').text()).toBe('Running')
    expect(wrapper.find('.running-spinner').exists()).toBe(true)
    wrapper.unmount()
  })

  it('renders a stopped status as a terminal state without a spinner', async () => {
    const store = useProjectStore()
    store.subagents = {
      [CHAT_ID]: [transcript({ status: 'stopped' })],
    }

    const wrapper = await mountView()

    expect(wrapper.get('.status-value').text()).toBe('Stopped')
    expect(wrapper.get('.status-value').classes()).toContain('stopped')
    expect(wrapper.find('.running-spinner').exists()).toBe(false)
    wrapper.unmount()
  })
})
