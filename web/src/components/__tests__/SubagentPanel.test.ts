// @vitest-environment jsdom

import { describe, expect, it } from 'vitest'
import { mount, RouterLinkStub } from '@vue/test-utils'
import type { SubagentTranscript } from '../../lib/types'
import { subagentPath } from '../../lib/subagentIds'
import SubagentPanel from '../SubagentPanel.vue'
import ActivityGlyph from '../ActivityGlyph.vue'

function sub(over: Partial<SubagentTranscript> = {}): SubagentTranscript {
  return {
    agent_id: 'agent-a1',
    description: 'Explore repo layout',
    subagent_type: 'Explore',
    status: 'completed',
    messages: [],
    ...over,
  }
}

function msg(timestamp: string, content = 'hello') {
  return { role: 'assistant' as const, content, timestamp }
}

function mountPanel(subagents: SubagentTranscript[], chatId?: string) {
  return mount(SubagentPanel, {
    props: { subagents, chatId },
    global: { stubs: { RouterLink: RouterLinkStub } },
  })
}

describe('SubagentPanel', () => {
  it('renders one row per subagent with description, type and status word', () => {
    const w = mountPanel([
      sub(),
      sub({ agent_id: 'b2', description: 'Write tests', subagent_type: 'general', status: 'running' }),
    ])
    const rows = w.findAll('.subagent-row')
    expect(rows).toHaveLength(2)
    expect(rows[0].find('.subagent-row-name').text()).toBe('Explore repo layout')
    expect(rows[0].find('.subagent-row-type').text()).toBe('Explore')
    expect(rows[0].find('.subagent-row-status').text()).toBe('finished')
    expect(rows[1].find('.subagent-row-status').text()).toBe('running')
  })

  it('shows the glyph only for a running row', () => {
    const w = mountPanel([sub(), sub({ agent_id: 'b2', status: 'running' })])
    const rows = w.findAll('.subagent-row')
    expect(rows[0].findComponent(ActivityGlyph).exists()).toBe(false)
    expect(rows[1].findComponent(ActivityGlyph).exists()).toBe(true)
  })

  it('marks a failed row with the failed class and word', () => {
    const w = mountPanel([sub({ status: 'failed' })])
    const row = w.get('.subagent-row')
    expect(row.classes()).toContain('failed')
    expect(row.get('.subagent-row-status').text()).toBe('failed')
  })

  it('renders the duration from first and last message timestamps', () => {
    const w = mountPanel([
      sub({ messages: [msg('2026-10-06T10:00:00Z'), msg('2026-10-06T10:02:10Z')] }),
      sub({ agent_id: 'b2', messages: [msg('2026-10-06T10:00:00Z')] }),
    ])
    const rows = w.findAll('.subagent-row-status')
    expect(rows[0].text()).toContain('2m 10s')
    expect(rows[1].text()).toBe('finished')
  })

  it('links to the read-only transcript, and omits the link without a chatId', () => {
    const w = mountPanel([sub()], 'c1')
    const link = w.getComponent(RouterLinkStub)
    expect(link.props('to')).toBe(subagentPath('c1', 'agent-a1'))
    expect(link.text()).toContain('Open')
    expect(mountPanel([sub()]).findComponent(RouterLinkStub).exists()).toBe(false)
  })

  it('renders no transcript content', () => {
    const w = mountPanel([sub({ messages: [msg('2026-10-06T10:00:00Z', 'SECRET-TRANSCRIPT-TEXT')] })], 'c1')
    expect(w.html()).not.toContain('SECRET-TRANSCRIPT-TEXT')
  })
})
