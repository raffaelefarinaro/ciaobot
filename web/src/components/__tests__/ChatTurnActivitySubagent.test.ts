// @vitest-environment jsdom

import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import ChatTurnActivity from '../ChatTurnActivity.vue'
import { subagentCardFromHistory } from '../../lib/subagentCard'
import type { ChatMessage } from '../../lib/types'

const card = (wire: Record<string, unknown>) => subagentCardFromHistory(wire) ?? undefined

const mountSteps = (steps: ChatMessage[]) => mount(ChatTurnActivity, {
  props: {
    steps,
    open: true,
    outputsId: 'out-1',
    thinkingExpanded: false,
    renderMarkdown: (text: string) => text,
    renderActivityLine: (line: string) => line,
  },
})

describe('ChatTurnActivity history subagent rows', () => {
  it('renders a stored subagent card from history with its nested rows', () => {
    const wrapper = mountSteps([
      { role: 'system', content: 'Subagent: Scan', tool_name: '_subagent', timestamp: '', subagent: card({
        tool_use_id: 'sa1', agent_type: 'general', description: 'Scan', state: 'done', step_count: 1,
        steps: [{ kind: 'activity', line: '$ ls', tool_use_id: 'k1' }],
      }) },
    ])

    expect(wrapper.get('.trace-step--subagent .subagent-card-title').text()).toBe('Subagent: Scan')
    expect(wrapper.get('.subagent-card-state-label').text()).toBe('Done')
    expect(wrapper.find('.subagent-card-toggle').attributes('aria-expanded')).toBe('false')
  })

  it('shows a journal from before cards as a plain Subagent card and does not crash', () => {
    const wrapper = mountSteps([
      { role: 'system', content: 'Subagent', tool_name: '_subagent', timestamp: '', subagent: card({ tool_use_id: 'old', state: 'done' }) },
      { role: 'system', content: 'Subagent', tool_name: '_subagent', timestamp: '' },
    ])

    expect(wrapper.findAll('.subagent-card-title').map(node => node.text())).toEqual(['Subagent'])
  })
})
