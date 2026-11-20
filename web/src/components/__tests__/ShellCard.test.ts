// @vitest-environment jsdom

import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import { nextTick } from 'vue'
import ShellCard from '../ShellCard.vue'
import type { ShellCard as Card } from '../../lib/shellCard'

const card = (overrides: Partial<Card> = {}): Card => ({
  tool_use_id: 'sh1',
  command: 'npm run build',
  background: false,
  state: 'running',
  exit_code: null,
  output_tail: 'building...',
  ...overrides,
})

describe('ShellCard', () => {
  it('opens while running and shows the command, the state and the output as text', () => {
    const wrapper = mount(ShellCard, { props: { card: card({ output_tail: '<img src=x onerror=alert(1)>' }) } })
    const toggle = wrapper.get('.shell-card-toggle')

    expect(toggle.attributes('aria-expanded')).toBe('true')
    expect(wrapper.get('.shell-card-command').text()).toBe('npm run build')
    expect(wrapper.get('.shell-card-state-label').text()).toBe('Running')
    // The tail is text: markup in the output is shown literally, never rendered.
    expect(wrapper.get('.shell-card-output').text()).toBe('<img src=x onerror=alert(1)>')
    expect(wrapper.find('.shell-card-output img').exists()).toBe(false)
  })

  it('folds once a finished card settles, and the exit code sits beside the state', async () => {
    const wrapper = mount(ShellCard, { props: { card: card() } })
    await wrapper.setProps({ card: card({ state: 'done', exit_code: 0, output_tail: 'ok' }) })

    expect(wrapper.get('.shell-card-toggle').attributes('aria-expanded')).toBe('false')
    expect(wrapper.find('.shell-card-body').exists()).toBe(false)
    expect(wrapper.get('.shell-card-state-label').text()).toBe('Done')
    expect(wrapper.get('.shell-card-exit').text()).toBe('exit 0')
  })

  it('opens again on a click and shows the output of a finished card', async () => {
    const wrapper = mount(ShellCard, { props: { card: card({ state: 'done', exit_code: 0, output_tail: 'ok' }) } })
    await wrapper.get('.shell-card-toggle').trigger('click')
    expect(wrapper.get('.shell-card-toggle').attributes('aria-expanded')).toBe('true')
    expect(wrapper.get('.shell-card-output').text()).toBe('ok')
  })

  it('marks a failed card with the failed border and its non-zero exit code', () => {
    const wrapper = mount(ShellCard, { props: { card: card({ state: 'failed', exit_code: 2 }) } })
    expect(wrapper.classes()).toContain('shell-card--failed')
    expect(wrapper.get('.shell-card-state-label').text()).toBe('Failed')
    expect(wrapper.get('.shell-card-exit').text()).toBe('exit 2')
  })

  it('shows the background badge only while a backgrounded shell runs', async () => {
    const wrapper = mount(ShellCard, { props: { card: card({ background: true, output_tail: '' }) } })
    expect(wrapper.get('.shell-card-badge').text()).toBe('running in the background')
    expect(wrapper.get('.shell-card-badge').attributes('title')).toContain('reports back')
    expect(wrapper.get('.shell-card-body').text()).toBe('No output yet.')

    await wrapper.setProps({ card: card({ background: true, state: 'done', exit_code: 0 }) })
    await nextTick()
    expect(wrapper.find('.shell-card-badge').exists()).toBe(false)
  })

  it('titles a call with no command yet as Shell', () => {
    const wrapper = mount(ShellCard, { props: { card: card({ command: '', output_tail: '' }) } })
    expect(wrapper.get('.shell-card-command').text()).toBe('Shell')
  })

  it('keeps the disclosure keyboard reachable and the output focusable for scrolling', () => {
    const wrapper = mount(ShellCard, { props: { card: card() } })
    expect(wrapper.get('.shell-card-toggle').element.tagName).toBe('BUTTON')
    expect(wrapper.get('.shell-card-output').attributes('tabindex')).toBe('0')
  })
})
