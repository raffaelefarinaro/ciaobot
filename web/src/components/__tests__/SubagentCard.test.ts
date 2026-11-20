// @vitest-environment jsdom

import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import SubagentCard from '../SubagentCard.vue'
import type { SubagentCard as Card, SubagentStep } from '../../lib/subagentCard'

const card = (overrides: Partial<Card> = {}): Card => ({
  tool_use_id: 'sa1',
  agent_type: 'general',
  description: 'Sleep 5 then reply done',
  state: 'running',
  step_count: 1,
  steps: [{ kind: 'activity', line: '$ sleep 5', tool_use_id: 'k1' }],
  ...overrides,
})

const toggle = (wrapper: ReturnType<typeof mount>) => wrapper.get('.subagent-card-toggle')

describe('SubagentCard', () => {
  it('opens while running and shows the header with its type, state and step count', () => {
    const wrapper = mount(SubagentCard, { props: { card: card() } })
    const button = toggle(wrapper)

    expect(button.attributes('aria-expanded')).toBe('true')
    expect(button.get('.subagent-card-title').text()).toBe('Subagent: Sleep 5 then reply done')
    expect(button.get('.subagent-card-title').attributes('title')).toBe('Subagent: Sleep 5 then reply done')
    expect(button.get('.subagent-card-type').text()).toBe('general')
    expect(button.get('.subagent-card-state-label').text()).toBe('Running')
    expect(button.get('.subagent-card-count').text()).toBe('1 step')
    expect(button.find('.working-indicator, .activity-glyph--working').exists()).toBe(true)
    expect(wrapper.get('#' + button.attributes('aria-controls')).text()).toContain('$ sleep 5')
  })

  it('folds once a finished card settles, and the reader can open it again', async () => {
    const wrapper = mount(SubagentCard, { props: { card: card() } })
    await wrapper.setProps({ card: card({ state: 'done' }) })
    expect(toggle(wrapper).attributes('aria-expanded')).toBe('false')
    expect(wrapper.find('.subagent-card-body').exists()).toBe(false)

    await toggle(wrapper).trigger('click')
    expect(toggle(wrapper).attributes('aria-expanded')).toBe('true')
    expect(wrapper.get('.subagent-card-body').text()).toContain('$ sleep 5')
  })

  it('shows a finished card collapsed, and a failed one with its failure in words', () => {
    const done = mount(SubagentCard, { props: { card: card({ state: 'done' }) } })
    expect(toggle(done).attributes('aria-expanded')).toBe('false')
    expect(toggle(done).get('.subagent-card-state-label').text()).toBe('Done')

    const failed = mount(SubagentCard, { props: { card: card({ state: 'failed' }) } })
    expect(failed.classes()).toContain('subagent-card--failed')
    expect(toggle(failed).get('.subagent-card-state-label').text()).toBe('Failed')
    expect(toggle(failed).attributes('aria-expanded')).toBe('false')
  })

  it('draws the nested steps as text, never as markup', () => {
    const hostile: SubagentStep = { kind: 'activity', line: '<img src=x onerror="alert(1)"> ls', tool_use_id: 'k1' }
    const wrapper = mount(SubagentCard, { props: { card: card({ steps: [hostile], step_count: 1 }) } })

    expect(wrapper.find('img').exists()).toBe(false)
    expect(wrapper.get('.activity-line').text()).toBe('<img src=x onerror="alert(1)"> ls')
  })

  it('bounds a card with many children and counts the rest', () => {
    // The live card may hold more steps than the view draws: the view still stops at fifty.
    const steps: SubagentStep[] = Array.from({ length: 60 }, (_, n) => ({ kind: 'activity', line: `cmd ${n}`, tool_use_id: `k${n}` }))
    const wrapper = mount(SubagentCard, { props: { card: card({ steps, step_count: 73 }) } })

    expect(wrapper.findAll('.activity-line')).toHaveLength(50)
    expect(wrapper.get('.subagent-card-more').text()).toBe('+23 more')
    expect(toggle(wrapper).get('.subagent-card-count').text()).toBe('73 steps')
  })

  it('opens a file step through the parent, with its path as the title', async () => {
    const file: SubagentStep = { kind: 'file', file_path: '/w/notes.md', action: 'created', tool: 'Write', tool_use_id: 'k2' }
    const wrapper = mount(SubagentCard, { props: { card: card({ steps: [file], step_count: 1 }) } })
    const button = wrapper.get('.subagent-card-file')

    expect(button.attributes('title')).toBe('/w/notes.md')
    await button.trigger('click')
    expect(wrapper.emitted('open-file')).toEqual([['/w/notes.md']])
  })

  it('wires the header as a real disclosure button', async () => {
    const wrapper = mount(SubagentCard, { props: { card: card() } })
    expect(toggle(wrapper).element.tagName).toBe('BUTTON')
    expect(toggle(wrapper).attributes('type')).toBe('button')
    await nextTick()
    expect(toggle(wrapper).attributes('aria-controls')).toBeTruthy()
  })
})
