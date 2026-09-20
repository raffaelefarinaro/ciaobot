// @vitest-environment jsdom
//
// The completed-turn Activity row, mounted on its own. No ChatPanel, no store:
// the component takes the turn as props and reports every action as an emit.

import { describe, it, expect } from 'vitest'
import { mount } from '@vue/test-utils'
import ChatTurnActivity from '../ChatTurnActivity.vue'
import type { ChatMessage } from '../../lib/types'

function step(partial: Partial<ChatMessage>): ChatMessage {
  return { role: 'assistant', content: '', ...partial } as ChatMessage
}

function mountRow(props: Record<string, unknown> = {}) {
  return mount(ChatTurnActivity, {
    props: {
      steps: [step({ content: 'Checking the config' })],
      open: false,
      chatId: 'chat-1',
      thinkingExpanded: false,
      renderMarkdown: (text: string) => `<p>${text}</p>`,
      renderActivityLine: (line: string) => line,
      ...props,
    },
    global: { stubs: { SubagentPanel: true } },
  })
}

describe('ChatTurnActivity', () => {
  it('renders a collapsed Activity summary and asks the parent to toggle it', async () => {
    const wrapper = mountRow()

    const summary = wrapper.get('button.trace-summary')
    expect(summary.text()).toContain('Activity')
    expect(summary.attributes('aria-expanded')).toBe('false')
    expect(wrapper.find('.trace-body').exists()).toBe(false)

    await summary.trigger('click')
    expect(wrapper.emitted('toggle')).toHaveLength(1)
    // The open flag stays with ChatPanel: nothing changed locally.
    expect(wrapper.find('.trace-body').exists()).toBe(false)
  })

  it('renders the body through the markdown renderer it was handed', () => {
    const wrapper = mountRow({ open: true })

    expect(wrapper.get('button.trace-summary').attributes('aria-expanded')).toBe('true')
    expect(wrapper.get('.trace-body .trace-text').html()).toContain('<p>Checking the config</p>')
  })

  it('splits an activity step into lines and marks subagent lines', () => {
    const wrapper = mountRow({
      open: true,
      steps: [step({ tool_name: '_activity', content: 'Read config.py\n\n↳ [Explore] searching\n' })],
    })

    const lines = wrapper.findAll('.trace-tools .activity-line')
    expect(lines.map(l => l.text())).toEqual(['Read config.py', '↳ [Explore] searching'])
    expect(lines[0].classes()).not.toContain('subagent')
    expect(lines[1].classes()).toContain('subagent')
  })

  it('reports a file card click as an open-file request', async () => {
    const wrapper = mountRow({
      open: true,
      steps: [step({ tool_name: '_filecard', file_path: 'notes/plan.md', action: 'created' })],
    })

    const card = wrapper.get('button.file-card')
    expect(card.text()).toContain('plan.md')
    expect(card.text()).toContain('created')
    expect(card.text()).toContain('notes')

    await card.trigger('click')
    expect(wrapper.emitted('open-file')).toEqual([['notes/plan.md']])
  })

  it('renders output chips, labels new files and reports clicks', async () => {
    const wrapper = mountRow({
      open: true,
      outputs: [
        { file_path: 'a/report.md', action: 'created' },
        { file_path: 'b/notes.md', action: 'edited' },
      ],
    })

    const chips = wrapper.findAll('.trace-files .file-chip')
    expect(chips).toHaveLength(2)
    expect(chips[0].text()).toContain('new')
    expect(chips[1].text()).not.toContain('new')

    await chips[1].trigger('click')
    expect(wrapper.emitted('open-file')).toEqual([['b/notes.md']])
  })

  it('keeps a collapsed thinking block collapsed and defers the preference to the parent', async () => {
    const wrapper = mountRow({
      open: true,
      steps: [step({ tool_name: '_thinking', content: 'weighing options' })],
    })

    expect(wrapper.find('.trace-thinking').exists()).toBe(false)
    await wrapper.get('button.thinking-toggle').trigger('click')
    expect(wrapper.emitted('toggle-thinking')).toHaveLength(1)
  })

  it('offers to load a lazily stored reasoning step instead of rendering it', async () => {
    const lazyStep = step({ tool_name: '_thinking', content: '', lazy: true, i: 7 })
    const wrapper = mountRow({ open: true, thinkingExpanded: true, steps: [lazyStep] })

    await wrapper.get('button.thinking-load').trigger('click')
    expect(wrapper.emitted('expand-step')).toEqual([[lazyStep]])
  })

  it('forwards a body click so the parent can decide whether to collapse', async () => {
    const wrapper = mountRow({ open: true })

    await wrapper.get('.trace-body').trigger('click')
    expect(wrapper.emitted('body-click')).toHaveLength(1)
  })
})
