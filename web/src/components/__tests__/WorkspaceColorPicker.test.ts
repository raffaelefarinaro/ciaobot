// @vitest-environment jsdom
import { expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import WorkspaceColorPicker from '../settings/WorkspaceColorPicker.vue'
import type { WorkspaceColorId } from '../../lib/workspaceColors'

function build(modelValue: WorkspaceColorId, extra: Record<string, unknown> = {}) {
  return mount(WorkspaceColorPicker, { props: { modelValue, labelledby: 'lbl', ...extra } })
}

it('shows the five base swatches and a collapsed More colours disclosure', () => {
  const wrapper = build('amber')
  expect(wrapper.get('[role="radiogroup"]').attributes('aria-labelledby')).toBe('lbl')
  expect(wrapper.findAll('[role="radio"]').map((r) => r.attributes('aria-label'))).toEqual([
    'Ciao Orange', 'Rose Pink', 'Terminal Cyan', 'Emerald Mint', 'Violet Iris',
  ])
  const more = wrapper.get('.workspace-color-swatch--more')
  expect(more.attributes('role')).toBeUndefined()
  expect(more.attributes('aria-expanded')).toBe('false')
  expect(more.attributes('aria-label')).toBe('More colours')
})

it('the plus button reveals the four extra presets and flips aria-expanded', async () => {
  const wrapper = build('amber')
  await wrapper.get('.workspace-color-swatch--more').trigger('click')
  expect(wrapper.findAll('[role="radio"]')).toHaveLength(9)
  const more = wrapper.get('.workspace-color-swatch--more')
  expect(more.attributes('aria-expanded')).toBe('true')
  expect(more.attributes('aria-label')).toBe('Show fewer colours')
  await more.trigger('click')
  expect(wrapper.findAll('[role="radio"]')).toHaveLength(5)
})

it('mounts expanded when the value is an extra preset', () => {
  const wrapper = build('teal')
  expect(wrapper.findAll('[role="radio"]')).toHaveLength(9)
  expect(wrapper.get('[aria-label="Deep Teal"]').attributes('aria-checked')).toBe('true')
  expect(wrapper.get('.workspace-color-swatch--more').attributes('aria-expanded')).toBe('true')
})

it('emits update:modelValue when a swatch is clicked', async () => {
  const wrapper = build('amber')
  await wrapper.get('[aria-label="Rose Pink"]').trigger('click')
  expect(wrapper.emitted('update:modelValue')).toEqual([['pink']])
})

it('disables every swatch and the disclosure', () => {
  const wrapper = build('amber', { disabled: true })
  for (const button of wrapper.findAll('button')) expect(button.attributes('disabled')).toBeDefined()
})
